# Kết quả kiểm tra — 09/10/2026

- Cài đặt thành công với Python 3.12, beautifulsoup4 4.15.0, protego 0.7.0,
  defusedxml 0.7.1. File Python biên dịch được, CLI --help hoạt động.
- Lấy trực tiếp HTML https://cellphones.com.vn/iphone-16.html thành công.
  Parser trích xuất 1 Product, 16 thông số, giá schema `22990000` VND.
  Đây là giá quan sát trong schema của lần lấy này, không phải báo giá bảo đảm.
- Đọc sitemap index thực của CellphoneS: 70 sitemap con.
- Chạy CLI thực với robots, một URL CellphoneS, SQLite: queue có trạng thái
  product, bảng products có 1 bản ghi. Lệnh export xuất JSONL đọc lại được.
- Kiểm tra nhỏ bằng assertion: từ chối URL khác origin; không đoán giá
  `12.990.000` thành số theo định dạng chưa xác định.
- Thử truy cập TGDD: lỗi HTTP 502; Phong Vũ robots: HTTP 403.
  Chưa xác minh parser trên HTML thực của hai nguồn này.
- Chưa chạy toàn catalogue; chưa xác minh độ bao phủ danh mục/biến thể, giao
  diện JS, cơ chế retry qua lỗi mạng thực hay recovery khi tiến trình bị kill.

`sample-cellphones.jsonl` là dữ liệu trích xuất từ HTML thực, không phải dữ liệu
tạo giả. Dữ liệu mẫu chỉ minh họa cấu trúc, không đại diện toàn catalogue.
