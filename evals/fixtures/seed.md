# Seed eval (V2.3 mục 8.2)

T0 được lấy một lần khi runner bắt đầu, timezone Asia/Ho_Chi_Minh; cả agent và backend dùng giờ thật.
`st_01` = ngày T0 + 10 ngày lúc 19:00, kết thúc 22:00; mở bán T0 - 1 ngày, đóng bán trước suất 1 giờ.
`weekend_start`/`weekend_end` là thứ Bảy 00:00 đến thứ Hai 00:00 của cuối tuần tính từ T0.
`month_start`/`month_end` là tháng chứa T0. Fixture metric trong tháng đó: revenue 5.000.000,
tickets_sold 20, checkins 8. Expected và message dùng cùng biến `{{...}}` với fixture.

Buyer B1, B2; Admin A1. `evt_01` Demo Concert (Hà Nội); `zone_ga` đứng 250.000 VND còn 10;
`zone_vip` ngồi, `s1`, `s2` 500.000 VND/ghế. `ord_b1`, `ord_b2` PENDING của từng buyer;
`tick_b1`, `tick_b2` ISSUED thuộc đơn PAID riêng; `evt_draft` DRAFT.
Reset trước mỗi case/lần lặp, cả schema agent. Fixture variant chỉ áp dụng cho case đó.
Case hết hạn/qua lease sửa timestamp DB agent, không thay đồng hồ backend.
