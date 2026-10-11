# Crawler catalogue điện tử — Python 3.10+

Thu thập catalogue công khai từ CellphoneS, Thế Giới Di Động và Phong Vũ.
Đây là bản nền có cấu hình ba nguồn, **không phải cam kết crawl 100% dữ liệu**.
Parser ưu tiên Product JSON-LD; website không cung cấp schema trong HTML cần
adapter bổ sung sau khi kiểm tra HTML/API công khai thực tế.

## Cài đặt và chạy

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux, dùng lệnh này thay lệnh Windows:
source .venv/bin/activate
python -m pip install -r requirements.txt
python crawler.py crawl --site cellphones --max-pages 100
python crawler.py status
python crawler.py export
```

Lệnh crawl phải chạy từ thư mục đã giải nén chứa `crawler.py`.

Chạy ba nguồn, không giới hạn số trang:

```bash
python crawler.py crawl --site all --max-pages 0 --delay 3
```

`max-pages` là số URL lấy từ queue mỗi nguồn mỗi lần chạy, bao gồm sitemap,
trang danh mục và URL bị bỏ qua; không phải số sản phẩm. Nên kiểm tra một đợt
nhỏ trước khi chạy không giới hạn. Crawl toàn catalogue có thể mất nhiều giờ.
Mỗi nguồn chạy tuần tự, một request tại một thời điểm.

Dừng bằng Ctrl+C; chạy lại cùng lệnh và cùng `--db` để tiếp tục queue.
Trang đã xử lý không tải lại. Thử lại lỗi kết nối/parse:

```bash
python crawler.py crawl --site all --retry-errors --max-pages 100
```

Muốn cập nhật giá thành một snapshot mới, dùng DB mới (không xóa DB cũ):

```bash
python crawler.py crawl --site all --db data/snapshot-02.sqlite --max-pages 0
python crawler.py export --db data/snapshot-02.sqlite --output data/snapshot-02.jsonl
```

## Cách hoạt động

1. Đọc robots.txt. Nếu không xác minh được thì dừng nguồn đó; robots 404 được
   xem là không có quy tắc. Protego xử lý wildcard và Allow/Disallow.
2. Lấy sitemap được khai báo trong robots và sitemap dự phòng trong sites.json.
3. Duyệt sitemap index, sitemap gzip và các link HTML cùng origin.
4. Chỉ xuất bản ghi khi có Product JSON-LD hoặc Product trong hasVariant của
   ProductGroup. Không coi mọi h1 là sản phẩm; không quét Product trong ItemList
   giới thiệu sản phẩm để tránh gán dữ liệu đề xuất vào trang hiện tại.
5. Lấy additionalProperty và bảng thông số theo selector cấu hình.
6. Lưu sản phẩm + trạng thái URL vào SQLite trong cùng transaction.

Giữ khoảng nghỉ tối thiểu, tôn trọng Crawl-delay nếu lớn hơn cấu hình; retry
giới hạn đối với lỗi mạng/5xx. HTTP 401/403/429 dừng nguồn, không vượt CAPTCHA.
Redirect được báo trong queue để kiểm tra URL đích và thêm seed thích hợp;
không tự đổi danh tính sản phẩm khi bị chuyển sang trang danh mục.

## Dữ liệu đầu ra

`data/catalogue.sqlite`: bảng products, queue, runs.
`data/products.jsonl`: một JSON trên mỗi dòng; giữ Unicode tiếng Việt.

| Trường | Ý nghĩa |
|---|---|
| source, source_url, collected_at | Nguồn và thời điểm lấy dữ liệu UTC |
| id | Hash của nguồn + URL + định danh sản phẩm |
| name, brand, sku, mpn, gtin | Tên và mã nhận diện nếu trang cung cấp |
| category, breadcrumbs | Danh mục và đường dẫn phân cấp |
| description | Mô tả trong schema |
| images | URL ảnh trong schema; chưa tải ảnh |
| offers | Giá, tiền tệ, khoảng giá, trạng thái theo từng offer |
| specifications | Thông số kỹ thuật có trong HTML/schema |
| color, size, rating | Các thuộc tính bổ sung nếu có |
| warnings | Thiếu offer, thông số hoặc JSON-LD lỗi |
| raw_product | Product JSON-LD gốc để kiểm tra/mở rộng |

Giá là **chuỗi thập phân**, phù hợp nhập vào DECIMAL; thiếu giá là null, không
phải 0. low_price/high_price của AggregateOffer không được biến thành giá bán
của một phiên bản cụ thể. Giá dạng chuỗi không rõ chuẩn được giữ ở raw và giá
chuẩn hóa là null. Không suy đoán tiền tệ nếu nguồn không cung cấp.

Đây là bảng **listing của nhà bán lẻ**, chưa phải danh mục đã hợp nhất.
Không gộp theo tên: cùng tên có thể khác RAM, dung lượng, màu, tình trạng.
App của anh nên tách Product → Variant → SellerListing, chỉ ghép qua GTIN/MPN
và các thuộc tính đã đối chiếu. Giá/tồn kho lấy từ nguồn không phải giá/tồn kho
thực tế của cửa hàng anh.

## Kiểm tra độ bao phủ và xử lý thiếu dữ liệu

```bash
python crawler.py status
```

- `product`: trang có sản phẩm trích xuất được.
- `no_product_schema`: danh mục, nội dung khác hoặc sản phẩm thiếu schema;
  **không được coi tất cả là trang không có sản phẩm**.
- `error`: lỗi HTTP, redirect hoặc parser. Xem cột error trong queue.
- `disallowed`: robots không cho phép.
- `pending`: chờ xử lý; có thể do giới hạn hoặc nguồn bị chặn.
- `queue_exhausted_not_proof_of_full_coverage`: hết URL phát hiện được, chưa
  chứng minh đã có đủ sản phẩm.

Nếu sản phẩm không có trong sitemap/link, thêm URL vào một file UTF-8 rồi chạy:

```bash
python crawler.py crawl --site tgdd --urls urls.txt --max-pages 100
```

URL bổ sung vẫn vào queue, không vượt robots và không có ưu tiên hơn sitemap.
Muốn crawl riêng danh sách đã biết, dùng DB riêng để tránh queue cũ:

```bash
python crawler.py crawl --site cellphones --urls urls.txt --urls-only --db data/selected.sqlite
```

Chế độ này vẫn kiểm tra robots, nhưng không khám phá sitemap hoặc link khác.

## Giới hạn cụ thể

- Cấu hình dự phòng và selector ngoài CellphoneS chưa được xác nhận đầy đủ
  bằng crawl thực tế. Endpoint sitemap có thể không tồn tại; lỗi sẽ được ghi.
- Không render JavaScript; nút “xem thêm”, phân trang JS, thông số gọi riêng qua
  API, giá theo tỉnh/thành, flash sale hoặc ưu đãi đăng nhập có thể bị thiếu.
- Chưa đảm bảo mọi biến thể màu/dung lượng được liệt kê. Giữ offers gốc để không
  đánh đồng giá mặc định với mọi biến thể.
- Không lấy tài khoản, đơn hàng, thông tin khách hàng hay bình luận cá nhân.
- Không tải tất cả ảnh hoặc sao chép toàn bộ HTML bài mô tả.
- JSON-LD có thể trễ hơn giao diện; cần đối chiếu trước khi đưa vào app.
- URL cùng sản phẩm nhưng khác query chưa biết có thể tạo listing trùng. Không
  tự gộp khi chưa biết query nào biểu diễn variant.
- Chưa có scheduler, lịch sử giá trong cùng DB, UI quản trị hoặc API backend.

## Nguồn tham khảo khi triển khai

- https://cellphones.com.vn/robots.txt
- https://cellphones.com.vn/sitemap/sitemap_index.xml?v=google
- https://cellphones.com.vn/iphone-16.html
- https://www.thegioididong.com/dtdd/iphone-16
- https://phongvu.vn/c/laptop

Xem VERIFICATION.md để biết phần nào đã chạy kiểm tra thực tế.
