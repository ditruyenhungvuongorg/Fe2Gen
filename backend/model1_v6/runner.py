"""GPU runner for Model 1 v6 inside the web process. Loads the base model ONCE (same unsloth stack as training).

report -> 5-model x 3-epoch Qwen LoRA ensemble (verbatim findings + assertion, 3-of-5 majority, modifier extension)
-> E5 retriever + memory of doctor-reviewed findings (top-10 HPO) -> Qwen reranker (reorders the 10 candidates).
Scores only rank suggestions for doctor review; they are not probabilities or diagnoses.

All 16 LoRA adapters share one PEFT model of rank 32 / alpha 64. Rank-16 / alpha-32 adapters are zero-padded to rank 32:
both use scaling alpha/r = 2 and the padded rows/columns are zero, so B @ A is unchanged.
The model bundle (weights, tokenizer, reviewed-finding memory) lives outside the repository; see README.md here.
"""
import json
import time
from pathlib import Path
from threading import Lock

from . import contract as C

RANK, ALPHA = 32, 64
TARGETS = ['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj']


class Model1V6Runner:
    def __init__(self, bundle_dir=None):
        self.bundle = Path(bundle_dir) if bundle_dir else None
        self.adapter_path = self.bundle  # used by /api/status "model_configured"
        self.is_loaded = False
        self.lock = Lock()
        self.current = None

    # ------------------------------------------------------------------ loading
    def load_model(self):
        if self.is_loaded:
            return
        if not self.bundle or not (self.bundle / 'manifest.json').is_file():
            raise RuntimeError('Chưa cấu hình bundle Model 1 v6 (MODEL1_V6_BUNDLE).')
        from unsloth import FastLanguageModel  # must precede transformers
        import torch
        from peft import get_peft_model_state_dict
        from peft.utils.save_and_load import load_peft_weights
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError('Model 1 v6 cần GPU CUDA.')
        m = json.loads((self.bundle / 'manifest.json').read_text(encoding='utf-8'))
        self.manifest = m
        self.members = [(c, eps) for c, eps in m['extractor']['members']]
        self.need, self.extend = m['extractor']['need'], m['extractor']['extend']
        model, _ = FastLanguageModel.from_pretrained(model_name=m['base_model_dir'], max_seq_length=C.MAX_LENGTH, dtype=torch.bfloat16,
                                                     load_in_4bit=False, load_in_16bit=True)
        tok = AutoTokenizer.from_pretrained(self.bundle / 'tokenizer', local_files_only=True)
        tok.padding_side = 'left'
        if tok.pad_token_id is None:
            tok.pad_token = '<|endoftext|>'
        self.eos = sorted({tok.convert_tokens_to_ids('<|im_end|>'), tok.convert_tokens_to_ids('<|endoftext|>')})
        model.generation_config.eos_token_id = self.eos
        model.generation_config.pad_token_id = tok.pad_token_id
        model = FastLanguageModel.get_peft_model(model, r=RANK, lora_alpha=ALPHA, lora_dropout=0, bias='none', target_modules=TARGETS,
                                                 use_gradient_checkpointing=False, random_state=0)
        FastLanguageModel.for_inference(model)
        expected = get_peft_model_state_dict(model)
        self.weights = {}
        for name, rel in m['adapters'].items():
            cfg = json.loads((self.bundle / rel / 'adapter_config.json').read_text())
            if cfg['lora_alpha'] / cfg['r'] != ALPHA / RANK or cfg['r'] > RANK:
                raise RuntimeError('Adapter scaling mismatch: ' + name)
            w = load_peft_weights(str(self.bundle / rel), device='cpu')
            if set(w) != set(expected):
                raise RuntimeError('Adapter tensor names mismatch: ' + name)
            padded = {}
            for k, t in w.items():
                target = torch.zeros(expected[k].shape, dtype=expected[k].dtype)
                target[tuple(slice(0, s) for s in t.shape)] = t.to(expected[k].dtype)
                padded[k] = target.to('cuda')
            self.weights[name] = padded
        self.tok, self.model = tok, model
        self.letters = [tok.encode(x, add_special_tokens=False)[0] for x in C.LETTERS]
        self.rtok = AutoTokenizer.from_pretrained(self.bundle / 'retriever', local_files_only=True)
        self.renc = AutoModel.from_pretrained(self.bundle / 'retriever', torch_dtype=torch.bfloat16, local_files_only=True).to('cuda').eval()
        self.catalog = [json.loads(s) for s in (self.bundle / 'hpo_catalog.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
        self.ids = [c['hpo_id'] for c in self.catalog]
        self.names = {c['hpo_id']: c for c in self.catalog}
        index = {h: i for i, h in enumerate(self.ids)}
        memory = [json.loads(s) for s in (self.bundle / 'memory.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
        self.usage = json.loads((self.bundle / 'usage.json').read_text(encoding='utf-8'))
        self.docs = self._encode([c['document_text'] for c in self.catalog])
        self.mem = self._encode([x['query_text'] for x in memory])
        self.mem_hpo = torch.tensor([index[x['hpo_id']] for x in memory], device='cuda', dtype=torch.long)
        self.generate('Thai 20 tuần.', next(iter(self.weights)), seconds=90)  # CUDA warm-up
        self.is_loaded = True

    def use(self, name):
        if self.current != name:
            from peft import set_peft_model_state_dict
            set_peft_model_state_dict(self.model, self.weights[name])
            self.current = name

    def _encode(self, texts, batch=64):
        import torch
        import torch.nn.functional as F
        out = []
        with torch.inference_mode():
            for i in range(0, len(texts), batch):
                t = self.rtok(['query: ' + x for x in texts[i:i + batch]], padding=True, truncation=True, max_length=96,
                              return_tensors='pt').to('cuda')
                h = self.renc(**t).last_hidden_state
                w = t['attention_mask'].unsqueeze(-1).to(h.dtype)
                out.append(F.normalize(((h * w).sum(1) / w.sum(1).clamp_min(1e-9)).float(), p=2, dim=1))
        return torch.cat(out)

    # ------------------------------------------------------------------ steps
    def generate(self, text, adapter, seconds=30):
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList
        self.use(adapter)
        prompt = self.tok.apply_chat_template([{'role': 'system', 'content': C.SYSTEM}, {'role': 'user', 'content': text}],
                                              tokenize=False, add_generation_prompt=True, enable_thinking=False)
        enc = self.tok(prompt, return_tensors='pt', add_special_tokens=False).to('cuda')
        n = enc['input_ids'].shape[1]
        if n + C.MAX_NEW > C.MAX_LENGTH:
            raise ValueError('Đoạn quá dài cho một lần trích.')
        tok = self.tok

        class JsonStop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kw):
                raw = tok.decode(input_ids[0, n:], skip_special_tokens=True).strip()
                if not raw.endswith('}'):
                    return False
                try:
                    return isinstance(json.loads(raw), dict)
                except ValueError:
                    return False
        with torch.inference_mode():
            out = self.model.generate(**enc, max_new_tokens=C.MAX_NEW, max_time=seconds, do_sample=False, num_beams=1,
                                      eos_token_id=self.eos, pad_token_id=tok.pad_token_id, use_cache=True,
                                      stopping_criteria=StoppingCriteriaList([JsonStop()]))
        ids = out[0, n:].tolist()
        while ids and ids[-1] in self.eos:
            ids.pop()
        return tok.decode(ids, skip_special_tokens=False).strip()

    def _member(self, text, config, epochs):
        preds = []
        for e in epochs:
            raw = self.generate(text, f'{config}__epoch{e}')
            try:
                preds.append(dict(predicted=C.align(text, raw)))
            except (ValueError, TypeError, KeyError) as exc:
                if not str(exc).startswith('Ambiguous repeated'):
                    preds.append(dict(predicted=[], error=str(exc)))
                    continue
                try:  # repeated phrase: earliest in-order occurrence, flagged for the doctor
                    preds.append(dict(predicted=C.lenient_align(text, raw)))
                except (ValueError, TypeError, KeyError):
                    preds.append(dict(predicted=[], error=str(exc)))
        return C.vote(preds) if len(preds) > 1 else preds[0]

    def extract_findings(self, text):
        chunk_chars = self.manifest.get('chunk_chars', 1200)
        parts = C.chunks(text, chunk_chars) if len(text) > chunk_chars else [(0, text)]
        findings, errors = [], []
        for off, seg in parts:
            ms = [self._member(seg, c, eps) for c, eps in self.members]
            p = C.vote(ms, self.need)
            if self.extend:
                p = C.extend_modifiers(seg, p)
            if p.get('error'):
                errors.append(p['error'])
            findings += [dict(C.shift(x, off), context=seg) for x in p['predicted']]
        return findings, errors

    def rank_hpo(self, findings, top_k=5):
        import torch
        if not findings:
            return []
        q = self._encode([C.normalize_query(f['mention_text']) for f in findings])
        cat = q @ self.docs.T
        mem = torch.full_like(cat, -1.0).scatter_reduce(1, self.mem_hpo.expand(len(q), -1), q @ self.mem.T, reduce='amax')
        top = torch.maximum(cat, mem).topk(C.K, dim=1)
        ranked = [[self.ids[j] for j in row] for row in top.indices.tolist()]
        self.use(self.manifest['reranker_adapter'])
        out = []
        for f, cands, cs in zip(findings, ranked, top.values.tolist()):
            user = C.rerank_user(f['context'], f['mention_text'], f['assertion'], cands, self.names, self.usage)
            prompt = self.tok.apply_chat_template([{'role': 'system', 'content': C.RERANK_SYSTEM}, {'role': 'user', 'content': user}],
                                                  tokenize=False, add_generation_prompt=True, enable_thinking=False)
            enc = self.tok(prompt, return_tensors='pt', add_special_tokens=False).to('cuda')
            with torch.inference_mode():
                lp = torch.log_softmax(self.model(**enc).logits[0, -1].float(), dim=-1)[self.letters].tolist()[:len(cands)]
            order = C.apply_rerank(cands, lp)[:top_k]
            out.append([dict(id=h, retriever_cosine=round(cs[cands.index(h)], 4), reranker_logprob=round(lp[cands.index(h)], 3))
                        for h in order])
        return out

    def extract(self, text):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('Model đang bận.')
        try:
            self.load_model()
            t0 = time.monotonic()
            findings, errors = self.extract_findings(text)
            for f, r in zip(findings, self.rank_hpo(findings)):
                f['hpo_ranked'] = r
            for f in findings:
                f.pop('context', None)
            return dict(findings=findings, errors=errors, seconds=round(time.monotonic() - t0, 1))
        finally:
            self.lock.release()
