#!/usr/bin/env bash
# Share the GPU between the web service, training and local AI.
#   bash gpu.sh pause   - release Model 1 of the web (within ~10 s) and keep it off, e.g. before training
#   bash gpu.sh resume  - allow the web to load Model 1 again (only when a doctor logs in or extracts text)
#   bash gpu.sh status  - pause flag, web online, model state (state file) and GPU processes
set -euo pipefail
PAUSE="${MODEL1_PAUSE_FILE:-$HOME/prenatal_pubcasefinder/.deployment/gpu_pause}"
# Same default as model1_process.DEFAULT_STATE_FILE; MODEL1_STATE_FILE='' (no file) stays empty.
STATE="${MODEL1_STATE_FILE-$HOME/prenatal_pubcasefinder/.deployment/model1_state.json}"
PORT="${PORT:-8000}"
case "${1:-status}" in
  pause)
    mkdir -p "$(dirname "$PAUSE")"; touch "$PAUSE"
    echo "Đã tạm dừng Model 1 của web; VRAM được trả trong khoảng 10 giây. Tra cứu và đối chiếu vẫn chạy." ;;
  resume)
    rm -f "$PAUSE"
    echo "Web được dùng GPU lại; Model 1 chỉ nạp khi bác sĩ đăng nhập hoặc tách đoạn văn." ;;
  status)
    if [ -e "$PAUSE" ]; then echo "Cờ tạm dừng: CÓ ($PAUSE)"; else echo "Cờ tạm dừng: không"; fi
    # /api/status only says "online" now; the model state comes from the file the web process writes on each change.
    if curl -s -f -m 5 -o /dev/null "http://127.0.0.1:$PORT/api/status"; then echo "Web: online (cổng $PORT)"
    else echo "Không gọi được web ở cổng $PORT (file trạng thái dưới đây có thể đã cũ)"; fi
    if [ -n "$STATE" ] && [ -f "$STATE" ]; then
      python3 - "$STATE" <<'PY' || echo "Không đọc được file trạng thái $STATE"
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print("Model 1:", d.get("model_state"), "| nạp lần gần nhất:", d.get("model_load_seconds"), "giây | cập nhật:",
      d.get("updated"), "|", d.get("model_error") or "")
PY
    else
      echo "Model 1: chưa có file trạng thái (${STATE:-MODEL1_STATE_FILE rỗng}); web chưa ghi trạng thái model."
    fi
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null || true ;;
  *) echo "Dùng: bash gpu.sh pause|resume|status"; exit 2 ;;
esac
