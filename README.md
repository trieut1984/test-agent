# Trợ lý Pháp lý AI (Agent TVPL)

AI agent tự động theo dõi và tóm tắt văn bản pháp luật Việt Nam mới nhất.

---

## Problem

Luật, nghị định, thông tư Việt Nam được ban hành liên tục trên nhiều cổng thông tin khác nhau. Người làm kế toán, pháp chế, thuế phải thủ công vào từng trang web, đọc toàn văn để nắm bắt nội dung quan trọng — tốn nhiều giờ mỗi tuần và dễ bỏ sót văn bản mới.

---

## Users

- Kế toán, kiểm toán viên cần cập nhật quy định thuế (GTGT, TNDN, TNCN, ...)
- Pháp chế doanh nghiệp theo dõi luật, nghị định mới
- Nhà quản lý muốn nắm nhanh chính sách mà không đọc toàn văn

---

## Solution

Agent tự động cào dữ liệu từ 3 nguồn chính thức:
- **congbao.chinhphu.vn** — Công báo Chính phủ
- **luatvietnam.vn** — Cổng pháp luật tổng hợp
- **baochinhphu.vn** — Báo Chính phủ

Với mỗi văn bản mới, agent dùng LLM (GreenNode / VNG Cloud) để sinh **tóm tắt 3–5 điểm bằng tiếng Việt**, ưu tiên nội dung ảnh hưởng trực tiếp đến doanh nghiệp. Ngoài ra có **Kho Thuế** — kho lưu trữ riêng cho 8 nhóm sắc thuế, tự động highlight điểm đáng chú ý.

**Giá trị mang lại:**
- Từ vài giờ đọc thủ công → chưa đến 1 phút xem tóm tắt
- Không bỏ sót văn bản quan trọng (luật, nghị định được ưu tiên xếp hạng cao hơn)
- Kho Thuế persistent — tra cứu lại được, không cần chạy lại từ đầu

---

## How to run

### Yêu cầu
- Python 3.12+  hoặc Docker
- API key của GreenNode (VNG Cloud MaaS) — đăng ký tại [console.vngcloud.vn](https://console.vngcloud.vn)

### Chạy local (không Docker)

```bash
# 1. Cài thư viện
pip install -r requirements.txt

# 2. Tạo file .env
cp .env.example .env
# Điền GREENNODE_API_KEY vào .env

# 3. Chạy server
python app.py
# → http://localhost:8080
```

File `.env` tối thiểu cần có:
```
GREENNODE_API_KEY=your_api_key_here
```

### Chạy bằng Docker

```bash
docker build -t tro-ly-phap-ly-ai .

docker run -p 8080:8080 \
  -e GREENNODE_API_KEY=your_api_key_here \
  tro-ly-phap-ly-ai
```

### Sử dụng

Mở trình duyệt → `http://localhost:8080`

| Hành động | Cách làm |
|---|---|
| Chạy agent lấy văn bản mới | Bấm nút **Chạy Agent** trên UI, hoặc `POST /api/run` |
| Xem kết quả lần chạy gần nhất | `GET /api/result` |
| Xem Kho Thuế | Tab **Kho Thuế** trên UI, hoặc `GET /api/khotrue` |
| Làm mới Kho Thuế | `POST /api/khotrue/refresh` |
| Kiểm tra kết nối AI | `GET /api/test-ai` |

> Lần đầu chạy agent sẽ mất 1–3 phút do cần gọi AI để tóm tắt. Các lần sau nhanh hơn vì kết quả được cache.

---

## What to customize

| Muốn thay đổi | File | Chỗ cần sửa |
|---|---|---|
| Dùng model AI khác | `.env` | `GREENNODE_MODEL=Qwen/Qwen2.5-72B-Instruct` hoặc `google/gemma-4-31b-it` |
| Thêm/bớt nguồn cào dữ liệu | `scraper.py` | Hàm `scrape_source()`, thêm URL và hàm parse tương ứng |
| Thay đổi nhóm sắc thuế trong Kho Thuế | `scraper_tax.py` | Dict `TAX_CATEGORIES` — thêm key và từ khóa lọc |
| Điều chỉnh cách chấm điểm ưu tiên văn bản | `scraper.py` | Hàm `score_document()` — tăng/giảm điểm theo loại văn bản |
| Thay đổi prompt tóm tắt AI | `summarizer.py` | Biến `prompt` trong hàm `summarize_document()` và `generate_highlights()` |
| Số văn bản tóm tắt mỗi lần chạy | `app.py` | Tham số `max_new=12` trong route `/api/run` |
| Cổng server | `.env` | `PORT=8080` |
