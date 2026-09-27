# Train tầng phân loại cơ chế của Model 2

So sánh Naive Bayes, SVM, Random Forest, XGBoost để dự đoán ca thai nên
xét nghiệm theo hướng nào trước: CMA/Karyotype (NST, CNV), WES (đơn gen) hay
không do di truyền. Đầu vào là bảng ca do
`backend/tools/build_mechanism_dataset.py` tạo ra (cần Model 2 và dữ liệu HPO).
Script train chỉ cần `numpy`, `scikit-learn`, `xgboost`.

```bash
python -m pip install -r requirements.txt
python train_model2_mechanism.py --data dataset_co_che.csv --task xet_nghiem --out ket_qua_xet_nghiem
python train_model2_mechanism.py --data dataset_co_che.csv --task co_che --out ket_qua_co_che
```

- Kiểm định chéo phân tầng lặp lại (mặc định 5-fold × 20 lần), siêu tham số cố
  định, không tinh chỉnh trên fold.
- Có baseline để so: lớp đông nhất, ngẫu nhiên theo tỷ lệ, và cột Model 2 có
  điểm cao nhất (hiển thị 3 cột hiện tại, chưa train).
- Kết quả: `metrics.json` (macro-F1, balanced accuracy, recall từng lớp, độ
  quan trọng đặc trưng của Random Forest) và các mô hình `*.joblib`.

**Nhãn.** Cột `label_source` cho biết nguồn nhãn. `goi_y_bac_si` là nhóm suy
từ bệnh bác sĩ gợi ý đầu tiên, không phải kết quả xét nghiệm; số đo trên nhãn
này chỉ cho biết mô hình bắt chước gợi ý của bác sĩ tốt đến đâu. Khi có nhãn
bác sĩ xác nhận hoặc kết quả karyotype/CMA/WES, dựng lại bảng với
`--confirmed-labels` rồi train lại.
