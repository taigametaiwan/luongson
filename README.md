# Chuối Chiên + Lương Sơn Multi-Source Scanner v4.3.0

Một lần chạy quét hai nguồn Chuối Chiên và Lương Sơn/Hygenie, sau đó chỉ xuất một playlist chung `all_live.m3u`.

## Điểm mới v4.3.0

- Chuyển cửa sổ quét thành **-150 phút đến +240 phút** so với giờ bắt đầu trận.
- Cơ chế **HTTP-first**: đọc trang trận, iframe, `streamUrl`, `video/source` và cấu hình player rõ ràng bằng request dùng chung cookie trước khi mở tab Chromium.
- Chỉ dùng Chromium fallback cho trận gần giờ/đang diễn ra hoặc khi HTTP-first chưa đủ luồng.
- Trận còn trên 45 phút mà chưa lộ stream sẽ không mở Chromium; hệ thống lưu lịch kiểm tra lại.
- Delta state qua `chuoichien_state.json` và `hygenie_state.json`, tránh quét lại trận xa giờ ở mọi lần chạy.
- Cache kết quả kiểm tra stream trong cùng một lượt chạy, tránh probe lặp và giảm nguy cơ 429.
- Dừng sớm nếu HTTP-first đã xác minh đủ 2 mức chất lượng.
- Vẫn giới hạn tối đa 2 chất lượng tốt nhất cho mỗi trận/BLV.
- Chỉ công bố duy nhất `all_live.m3u`.

## Cấu trúc

```text
main.py
merger.py
sources/
  __init__.py
  hybrid_support.py
  chuoichien.py
  luongson.py
tests/
.github/workflows/update.yml
```

## Chạy

```bash
python -u main.py
```

Test riêng nguồn/trận:

```bash
python -u main.py --source chuoichien
python -u main.py --source luongson
python -u main.py "URL_TRẬN"
```

## File đầu ra

```text
all_live.m3u
all_live_debug.json
chuoichien_state.json
hygenie_state.json
```

Hai file state không phải playlist; chúng chỉ lưu lịch lần quét tiếp theo. Trong ứng dụng IPTV chỉ nhập URL raw của `all_live.m3u`.
