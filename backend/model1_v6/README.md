# Model 1 v6 — trích cụm biểu hiện + gợi ý HPO

Prototype nghiên cứu, **không phải công cụ chẩn đoán**. Bác sĩ quyết định mã HPO và trạng thái cuối cùng; điểm số chỉ để xếp hạng.

## Luồng xử lý

1. **Tách cụm** (`contract.py`, `runner.py`): 5 adapter Qwen3.5-4B LoRA × 3 epoch trích cụm **nguyên văn** + trạng thái
   (`present`/`suspected`). Mỗi model bỏ phiếu giữa 3 epoch, rồi đa số 3/5 giữa các model; nối từ chỉ bên/mức độ đứng ngay sau cụm.
   Vị trí ký tự do căn chỉnh chính xác (Qwen không sinh mã HPO hay vị trí). Báo cáo dài hơn 1.200 ký tự được chia đoạn ở ranh giới câu.
2. **Gợi ý HPO**: E5 (fine-tune) + bộ nhớ cụm bác sĩ đã gán mã → 10 ứng viên → reranker Qwen xếp lại → Top-5.
3. **Bác sĩ duyệt** trên web (từ điển HPO Việt–Anh ở `backend/resources/hpo_catalog_vi.json` để tra thêm), sau đó Model 2 xếp hạng bệnh.

Kết quả đánh giá (5-fold CV theo ca trên 195 ca, end-to-end span + trạng thái + HPO Top-1): F1 65,9%; test độc lập 31 ca: 62,0%.
Gợi ý HPO khi đã có cụm đúng: Top-1 90,7% (CV), 87,0% (test).

## Bundle model (không đưa lên GitHub)

Trọng số, tokenizer và bộ nhớ cụm đã duyệt (dữ liệu lâm sàng) nằm trong một thư mục riêng trên máy GPU:

```
python -m model1_v6.build_bundle --source prod --research ~/model1_improve_ubuntu_v5 \
    --prod-run ~/model1_improve_ubuntu_v5/runs/prod_all226_<run> --out ~/fe2gen_bundles/model1_v6_prod
```

Bật trên web bằng biến môi trường `MODEL1_V6_BUNDLE=<thư mục bundle>`; không đặt biến này thì web dùng extractor v3.8 như cũ.
Kiểm chứng runner tái lập đúng số đã báo cáo (bundle `--source eval`): `python -m model1_v6.verify_on_test ...`.
