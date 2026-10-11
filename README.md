# Vast Monitor (bản cloud)

> Bản chạy local trên Mac, kết nối SSH trực tiếp: xem [`local-ssh/`](local-ssh/).

Dashboard theo dõi thực nghiệm trên các máy vast.ai, chạy trên Vercel, không cần Mac bật.

```
máy vast (agent.py) ──HTTPS 443──▶ Vercel /api/push ──▶ Upstash Redis ◀── Vercel UI (điện thoại / laptop)
                    ◀── lệnh chờ (kill, xem log) ──┘
```

- Mỗi máy vast chạy một agent Python (chỉ dùng thư viện chuẩn), cứ 60 giây gửi lên: GPU, tiến trình, tmux, đuôi log và metrics của các run.
- Máy tự xuất hiện trên dashboard, không phải nhập IP, port hay SSH key.
- Instance bị **huỷ** trên vast.ai thì dữ liệu của máy đó tự bị xoá (xem mục cuối).

## 1. Deploy lên Vercel

1. Đưa thư mục này lên một repo GitHub (để private), rồi vào Vercel → **Add New → Project** → import repo. Framework Preset để **Other**, không cần build command.
   Hoặc dùng CLI: `npm i -g vercel && vercel` ngay trong thư mục.
2. Tạo Redis: trong project Vercel → **Storage → Create Database → Upstash (Redis)**, chọn gói Free → Connect với project. Vercel tự thêm `KV_REST_API_URL` và `KV_REST_API_TOKEN`.
   Nếu tạo trực tiếp trên upstash.com thì tự thêm `UPSTASH_REDIS_REST_URL` và `UPSTASH_REDIS_REST_TOKEN`.
3. **Settings → Environment Variables**, thêm:

   | Biến | Giá trị | Ghi chú |
   |---|---|---|
   | `AGENT_TOKEN` | chuỗi ngẫu nhiên (`openssl rand -hex 24`) | agent dùng để gửi dữ liệu |
   | `VIEW_TOKEN` | chuỗi ngẫu nhiên khác | mật khẩu đăng nhập dashboard |
   | `VAST_API_KEY` | API key của vast.ai (console.vast.ai → Account → API Keys) | để biết instance nào đã bị huỷ |
   | `CRON_SECRET` | chuỗi ngẫu nhiên | Vercel Cron dùng khi gọi `/api/cleanup` |

4. **Deployments → Redeploy** để nhận biến mới. Mở `https://<tên-app>.vercel.app`, nhập `VIEW_TOKEN`.

## 2. Cấu hình vast.ai (làm một lần)

console.vast.ai → **Account → Settings → Environment Variables**. Các biến này được đưa vào mọi instance:

```
VM_URL=https://<tên-app>.vercel.app
VM_TOKEN=<AGENT_TOKEN ở trên>
NTFY_TOPIC=<tên topic khó đoán, vd luan-vast-8f3k2>     # tuỳ chọn: thông báo về điện thoại
```

Muốn nhận thông báo trên điện thoại: cài app **ntfy**, subscribe đúng topic ở trên. Khi tiến trình hoặc run kết thúc, agent gửi thông báo thẳng tới đó, không cần mở web.

## 3. Gắn vào thực nghiệm

**Trong code Python** (khuyên dùng, có thêm biểu đồ metrics):

```python
# tải một lần: curl -fsSL $VM_URL/vm.py -o vm.py
from vm import vm_start, vm_log, vm_finish

run = f"phobert_lr{args.lr}_seed{args.seed}"
vm_start(run, config=vars(args))
try:
    for epoch in range(args.epochs):
        ...
        vm_log(run, epoch=epoch, loss=train_loss, val_f1=f1)
    vm_finish(run, "done")
except BaseException as e:
    vm_finish(run, "failed", e)
    raise
```

**Chỉ dùng lệnh shell** (bỏ vào onstart script hoặc trước khi chạy):

```bash
mkdir -p ~/.vast_monitor && curl -fsSL "$VM_URL/agent.py" -o ~/.vast_monitor/vast_agent.py \
  && nohup python3 ~/.vast_monitor/vast_agent.py >> ~/.vast_monitor/agent.log 2>&1 &
nohup python train.py > logs/train.log 2>&1 &     # redirect stdout ra file để dashboard tự đọc log
```

Agent chỉ chạy một bản nhờ file lock, nên gọi `vm_start` nhiều lần cũng không sao. Chạy local (không có `VM_URL`) thì mọi hàm `vm_*` không làm gì cả.

## Dữ liệu được xoá khi nào

| Tình huống | Kết quả |
|---|---|
| Máy vẫn gửi đều | Đang chạy / Rảnh |
| Quá 3 phút không gửi, vast.ai báo instance còn nhưng đã stop | "Đã dừng", giữ lại |
| Quá 3 phút không gửi, instance **không còn** trong tài khoản vast.ai | **xoá ngay** khi có người mở dashboard, hoặc lúc cron chạy hằng ngày |
| Không đặt `VAST_API_KEY` | hiện "Mất liên lạc", tự xoá sau 48 giờ không có dữ liệu |
| Bấm "Xoá khỏi dashboard" | xoá ngay; agent còn sống thì máy sẽ hiện lại ở lần gửi tiếp |

Gói Hobby của Vercel chỉ cho cron chạy mỗi ngày một lần (đặt 03:00 giờ VN trong `vercel.json`). Việc xoá chủ yếu diễn ra khi mở dashboard.

## Giới hạn

- Dừng tiến trình hay xem một file log mới sẽ trễ tối đa một chu kỳ (60 giây). Trong lúc đang theo dõi log, agent chuyển sang chu kỳ 5 giây.
- Gói free của Upstash có khoảng 500K lệnh/tháng. Mỗi lần gửi tốn khoảng 5 lệnh, nên một máy chạy 24/7 với chu kỳ 60 giây dùng khoảng 216K lệnh/tháng. Nếu thường chạy 3 máy trở lên cùng lúc thì đặt `VM_INTERVAL=120` trong biến môi trường của vast.
- `nvidia-smi` trong container vast thường không ghép được PID với tiến trình, nên VRAM theo tiến trình hay để trống. Số liệu GPU tổng vẫn đúng.
