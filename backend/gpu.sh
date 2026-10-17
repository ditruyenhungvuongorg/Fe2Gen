#!/usr/bin/env bash
# Share the GPU between the web service, training and local AI.
#   bash gpu.sh pause   - release Model 1 of the web (within ~10 s) and keep it off, e.g. before training
#   bash gpu.sh resume  - allow the web to load Model 1 again (only when a doctor logs in or extracts text)
#   bash gpu.sh status  - pause flag, model state and GPU processes
set -euo pipefail
PAUSE="${MODEL1_PAUSE_FILE:-$HOME/prenatal_pubcasefinder/.deployment/gpu_pause}"
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
    curl -s -m 5 "http://127.0.0.1:$PORT/api/status" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print("Model 1:", d.get("model_state"), "| nạp lần gần nhất:", d.get("model_load_seconds"), "giây |", d.get("model_error") or "")' \
      || echo "Không gọi được web ở cổng $PORT"
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null || true ;;
  *) echo "Dùng: bash gpu.sh pause|resume|status"; exit 2 ;;
esac
