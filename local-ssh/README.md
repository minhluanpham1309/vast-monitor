# Vast Monitor

Web app chạy local để theo dõi tiến trình thực nghiệm trên các máy vast.ai qua SSH.

## Chạy

```bash
cd vast-monitor
./run.sh            # lần đầu tự tạo .venv và cài fastapi, uvicorn, paramiko
```

Script tự mở http://127.0.0.1:8765. Muốn đổi port: `PORT=9000 ./run.sh`.

## Thêm máy

Bấm **+ Thêm máy** rồi dán lệnh SSH mà vast.ai đưa (vd `ssh -p 41234 root@123.45.67.89 -L 8080:localhost:8080`). Form tự điền IP, port, user. Key thì chọn một trong hai:

- Đường dẫn key trên máy bạn, vd `~/.ssh/id_ed25519` (nên dùng cách này)
- Dán nội dung private key: app lưu vào `data/keys/<id>.key` với quyền 600

Không nhập key thì app thử ssh-agent hoặc key mặc định.

## Hiển thị gì

- Mỗi máy một thẻ: trạng thái (Đang chạy / Rảnh / Lỗi kết nối), thanh util và VRAM của từng GPU, số tiến trình, uptime.
- Bấm vào thẻ để xem chi tiết:
  - GPU: util, VRAM, nhiệt độ, công suất.
  - Các tiến trình khớp regex (mặc định `python|torchrun|accelerate|deepspeed`), xếp theo cây cha–con, có thời gian chạy, CPU, RAM, thư mục làm việc.
  - Nút **Log**: nếu stdout được redirect ra file (`nohup python train.py > run.log &`), app tự tìm ra file đó.
  - Nút **tmux**: nếu tiến trình chạy trong tmux, app tự tìm đúng pane để xem màn hình.
  - Ô xem log: nhập đường dẫn file hoặc tên phiên tmux, tự cập nhật mỗi 5 giây, cảnh báo nếu log đứng yên quá 30 phút (thường là bị treo).
  - Nút **Dừng**: gửi TERM, INT hoặc KILL.
- Tiến trình nào biến mất giữa hai lần làm mới sẽ được ghi vào mục "Tiến trình đã kết thúc" và bật thông báo (bấm "Bật thông báo" để nhận notification của trình duyệt).

## Lưu ý

- Server chỉ bind `127.0.0.1`, không lộ ra ngoài mạng.
- `data/machines.json` lưu danh sách máy. Passphrase (nếu có) nằm dạng plain text trong file này.
- Host key được tự chấp nhận vì máy vast.ai đổi liên tục.
- Trong container vast.ai, PID mà `nvidia-smi` báo thường là PID của host, nên cột GPU theo từng tiến trình hay để trống. Số liệu GPU tổng vẫn đúng.
- Muốn nhận thông báo khi tiến trình kết thúc thì phải để tab mở, app không chạy nền.
