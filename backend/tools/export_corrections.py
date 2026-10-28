"""Turn the approved doctor corrections (corrections.json, or the file the admin page downloads) into the two
inputs of the next Model 1 bundle build. Standard library only; no training is run here.

    python tools/export_corrections.py --file ~/prenatal_pubcasefinder/.deployment/corrections.json --out /tmp/gop_y

writes into --out:
  memory_additions.jsonl            {"query_text", "hpo_id"} per line: extra memory rows for
                                    model1_v6/build_bundle.py (append to the bundle's memory rows)
  doctor_phrases_additions.json     [{"id", "vi", "en"}]: merge into data/doctor_clinical_phrases.json
                                    (same shape; the server reads that file at startup)
The input may be the live store ({"items": [...]}, approved items only are used) or an export downloaded from
Quản trị → Góp ý sửa mã ({"items": [{"mention_text", "hpo_id", ...}], "memory_rows", "doctor_phrases"}).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from corrections import export_payload  # noqa: E402


def load(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SystemExit(f'{path}: không có danh sách "items".')
    if 'memory_rows' in data and 'doctor_phrases' in data:  # already an export from the admin page
        return data
    approved = [i for i in items if isinstance(i, dict) and i.get('status') == 'approved']
    return export_payload(approved)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Xuất góp ý sửa mã đã duyệt cho lần build bundle Model 1 tiếp theo.')
    parser.add_argument('--file', required=True, help='corrections.json của máy chủ hoặc file tải từ trang quản trị')
    parser.add_argument('--out', required=True, help='thư mục ghi memory_additions.jsonl và doctor_phrases_additions.json')
    args = parser.parse_args(argv)
    data = load(args.file)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    memory = out / 'memory_additions.jsonl'
    phrases = out / 'doctor_phrases_additions.json'
    with memory.open('w', encoding='utf-8') as f:
        for row in data['memory_rows']:
            f.write(json.dumps({'query_text': row['query_text'], 'hpo_id': row['hpo_id']}, ensure_ascii=False) + '\n')
    phrases.write_text(json.dumps([{'id': p['id'], 'vi': p['vi'], 'en': p.get('en', '')} for p in data['doctor_phrases']],
                                  ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'{data["count"]} góp ý đã duyệt -> {len(data["memory_rows"])} dòng memory ({memory}), '
          f'{len(data["doctor_phrases"])} cụm bác sĩ ({phrases}).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
