# Admin Agent – bảng tra tool, API và dữ liệu

Tài liệu đối chiếu tool Admin theo thiết kế V2.3 với OpenAPI và SQL của backend. Các path bên dưới có tiền tố `/api/v1`. Field API là JSON của backend. Field model là dữ liệu sau khi agent lọc hoặc đổi tên, ví dụ `id` thành `venue_id`.

Nguồn tra cứu gồm [thiết kế V2.3](VINGO_AGENT_DESIGN_V2.3.md), [registry](../src/vingo_agent/agents/registry.py), [tool đọc](../src/vingo_agent/tools/admin_read.py), [tool đề xuất](../src/vingo_agent/tools/proposals.py), [backend client](../src/vingo_agent/clients/backend.py) và [OpenAPI backend](../../../backend/vingo-backend/contracts/vingo-openapi.yaml).

## 1. Tool đọc Admin

Các field ở cột cuối là projection **theo thiết kế**. Kiểm registry và phần thân hàm trước khi dùng. Checkout được đối chiếu vẫn có tool đọc dạng hàm khung.

| Tool | Method, path và operationId | Input chính | Field cấp cho model theo V2.3 |
|---|---|---|---|
| `list_venues` | GET `/admin/venues` · `listVenues` | `q?`, `cursor?`, `limit?`; API có thêm `city?` | `items[].venue_id, name, city` |
| `get_admin_venue` | GET `/admin/venues/{venue_id}` · `getAdminVenue` | `venue_id` | `venue_id, name, city`. Thiết kế có `status`; response `Venue` hiện không có field đó. |
| `list_seat_layouts` | GET `/admin/venues/{venue_id}/seat-layouts` · `listSeatLayouts` | `venue_id, cursor?, limit?`; API có `layout_type?` | `items[].layout_id, name, status`; trạng thái layout cần dựa vào nguồn đã kiểm chứng. |
| `get_seat_layout` | GET `/admin/seat-layouts/{layout_id}` · `getSeatLayout` | `layout_id` | `layout_id, venue_id, name, zones[].zone_id/name/capacity`. Backend hiện không trả `status` ở endpoint này. |
| `get_seat_layout_zone` | GET `/admin/seat-layouts/{layout_id}/zones/{zone_id}` · `getSeatLayoutZone` | `layout_id, zone_id` | Hàng ghế của khu ngồi hoặc sức chứa khu đứng |
| `get_admin_layout_tree` | GET `/admin/venues/{venue_id}/layout-tree` · `getAdminLayoutTree` | `venue_id, layout_type, cursor?, limit?` | Các phiên bản sơ đồ theo loại, trạng thái, overview và khu |
| `list_admin_events` | GET `/admin/events` · `listAdminEvents` | `q?, status?, cursor?, limit?`; API có `category?` | `items[].event_id, title, category, status` |
| `get_admin_event` | GET `/admin/events/{event_id}` · `getAdminEvent` | `event_id` | `event_id, title, category, status, showtimes[].showtime_id/starts_at/status/layout_id`; đọc response gốc trong code khi cần snapshot giá. |
| `get_seatmap` | GET `/showtimes/{showtime_id}/seatmap` · `getSeatmap` | `showtime_id, zone_id`, bộ lọc hàng nếu hỗ trợ | `seats[].seat_id, row, number, status`; tối đa 60 ghế theo thiết kế |
| `get_zone_prices_preview` | POST `/admin/events/{event_id}/zone-prices/preview` · `previewZonePrices` | `event_id, assignments[]` | Tổng số SKU và số SKU theo khu; POST này chỉ tính preview |
| `admin_list_orders` | GET `/admin/orders` · `adminListOrders` | `status?, event_id?, showtime_id?, q?, cursor?, limit?` | `order_id, code, status, event_title, starts_at, total_amount, created_at` |
| `admin_get_order` | GET `/admin/orders/{order_id}` · `adminGetOrder` | `order_id` | OrderView, `payments[].status/amount/last_response_code`, `history[].status/at` |
| `admin_list_tickets` | GET `/admin/tickets` · `adminListTickets` | `showtime_id?, order_id?, code?, status?, cursor?, limit?`; cần ít nhất một bộ lọc | TicketView gồm mã, trạng thái, suất và vị trí vé |
| `list_showtime_checkins` | GET `/admin/showtimes/{showtime_id}/checkins` · `listShowtimeCheckins` | `showtime_id, cursor?, limit?` | `ticket_code, result, at` |
| `list_audit_logs` | GET `/admin/audit-logs` · `listAuditLogs` | `entity?, entity_id?, action?, from?, to?, cursor?, limit?` | `action, entity, entity_id, actor_name, via_agent, at` |
| `get_admin_dashboard` | GET `/admin/dashboard` · `getAdminDashboard` | Khoảng giờ, nhóm `day\|month` | `from, to, timezone, computed_at, current, previous, on_sale_showtimes, revenue, tickets` |
| `query_metric` | GET `/admin/reports/metrics` · `queryMetric` | `metric, from, to, event_id?, showtime_id?`; agent dùng `group_by=none` | `metric, unit, from, to, computed_at, value` |
| `list_metrics` | Danh mục cố định trong code | — | `tickets_sold, revenue, fill_rate, checkins`, định nghĩa và đơn vị |

Projection đưa mã và trạng thái nghiệp vụ vào model. Email, số điện thoại, QR, `payment_url` và nội dung audit `before/after` cần giữ ngoài projection dành cho model.

## 2. Tool đề xuất và tool quyết định

Tool đề xuất trả `status=PROPOSED`, `action_id`, `expires_at` và dựng thẻ `ConfirmAction`. Bảng dưới ghi API được executor gọi sau khi người dùng xác nhận. Các tool còn ở thiết kế được liệt kê để tra contract.

| Tool | Method, path và operationId | Body hoặc dữ liệu cần đọc | Response nghiệp vụ |
|---|---|---|---|
| `propose_create_venue` | POST `/admin/venues` · `createVenue` | `name, address, city` | `Venue` |
| `propose_update_venue` | PATCH `/admin/venues/{venue_id}` · `updateVenue` | Trường sửa trong `name, address, city, district, lat, lng, rules`; đọc `getAdminVenue` | `Venue` |
| `propose_create_event` | POST `/admin/events` · `createEvent` | `title, category`, tùy chọn `description, seat_layout_id, home_team, away_team, league, round, poster_mobile_url, poster_desktop_url, max_tickets_per_order` | `EventDetail` |
| `propose_update_event` | PATCH `/admin/events/{event_id}` · `updateEvent` | Các field trong `EventPatchInput`; đọc `getAdminEvent` | `EventDetail` |
| `propose_create_showtime` | POST `/admin/events/{event_id}/showtimes` · `createShowtime` | `seat_layout_id, starts_at, ends_at, sale_opens_at, sale_closes_at` | `Showtime` |
| `propose_update_showtime` | PATCH `/admin/showtimes/{showtime_id}` · `updateShowtime` | Tập field sửa trong năm field của suất; đọc phần tử tương ứng ở `getAdminEvent.showtimes[]` | `Showtime` |
| `propose_update_price_tiers` | PUT `/admin/events/{event_id}/price-tiers` · `putPriceTiers` | Body là **mảng trực tiếp** `[{id?, name, price, color?}]`; đọc `getAdminEvent.price_tiers[]` | HTTP 200 body rỗng |
| `propose_add_tier_price` | POST `/admin/events/{event_id}/price-tiers/{tier_id}/prices` · `addSkuPrice` | `price, effective_from?, reason?`; kiểm `event.status` và ID hạng | `SkuPrice` |
| `propose_update_zone_prices` | PUT `/admin/events/{event_id}/zone-prices` · `putZonePrices` | Body là **mảng trực tiếp** `[{zone_id, price_tier_id, display_name?}]`; preview bộ gán mới, đọc `zone_prices[]` hiện tại | `ZoneSkuPreview` |
| `propose_publish_event` | POST `/admin/events/{event_id}/publish` · `publishEvent` | `event_id` ở path; đọc `getAdminEvent.status` | `EventDetail` |
| `propose_block_seats` | POST `/admin/showtimes/{showtime_id}/seats/block` · `blockSeats` | `seat_ids, reason` | Thành công body rỗng |
| `propose_unblock_seats` | POST `/admin/showtimes/{showtime_id}/seats/unblock` · `unblockSeats` | `seat_ids` | Thành công body rỗng |
| `propose_admin_resend_ticket` | POST `/admin/tickets/{ticket_id}/resend` · `adminResendTicket` | `ticket_id` ở path; kiểm vé `ISSUED` | Thành công body rỗng |
| `propose_seat_layout` | POST `/admin/venue-imports/{session_id}/complete` · `completeVenueImport` | Session import, revision và preview đã chọn; SVG theo luồng upload | `venue_id, overview_id, layout_id` |
| `propose_cancel_event` | POST `/admin/events/{event_id}/cancel` · `cancelEvent` | `reason`; thẻ yêu cầu cụm xác nhận | Thành công body rỗng |
| `propose_cancel_showtime` | POST `/admin/showtimes/{showtime_id}/cancel` · `cancelShowtime` | `reason`; thẻ yêu cầu cụm xác nhận | Thành công body rỗng |
| `propose_update_event_visibility` | PUT `/admin/events/{event_id}/visibility` · `updateEventVisibility` | `visible, reason` | `EventDetail`; route có trong OpenAPI backend đang đối chiếu, tool vẫn cần tích hợp agent |
| `confirm_pending_action` | Luồng xác nhận nội bộ của agent | Không nhận input từ model. Đọc `visible_action.action_id/version` của turn. | Trạng thái thực thi và mã kết quả |
| `reject_pending_action` | Luồng từ chối nội bộ của agent | Không nhận input từ model. Đọc `visible_action.action_id/version`. | Chuyển proposal sang `REJECTED` |

## 3. Field response và cột DB

### Venue — danh sách và chi tiết sân

`GET /admin/venues` trả `data[]` và `next_cursor`; GET chi tiết trả một object. POST tạo sân và PATCH sửa sân dùng cùng mapper `Venue`.

| Field JSON | Cột hoặc phép tính |
|---|---|
| `id`, `name`, `address`, `district`, `lat`, `lng`, `rules` | Các cột cùng tên của bảng `venue`. `rules` có thể null. |
| `city` | `venue.city`, mapper dùng mã đã chuẩn hóa, ví dụ `HN`, `HCM`, `DN` |
| `city_name` | Nhãn hiển thị do backend suy ra từ mã thành phố |
| `created_at`, `updated_at` | Timestamp cùng tên trên `venue` |
| `layout_count` | Đếm `seat_map` theo `venue_id` |
| `published_layout_count` | Cùng giá trị với `layout_count` trong mapper |
| `draft_layout_count` | Đếm `venue_import_session` theo `venue_id`. Query hiện dùng điều kiện venue ID. |

Query danh sách lọc `venue.deleted_at IS NULL`, hỗ trợ tìm theo tên, lọc thành phố và phân trang theo `(created_at, id)`. PATCH ghép với bản ghi cũ; Java hiện giữ giá trị cũ nếu field PATCH là null. [SQL venue](../../../backend/vingo-backend/src/main/java/vingo/catalog/internal/VenueRepository.java) · [VenueService](../../../backend/vingo-backend/src/main/java/vingo/catalog/VenueService.java)

### Event — danh sách và chi tiết

- `GET /admin/events` lấy từ `event`: `id, title, category, status, visible, poster_mobile_url, poster_desktop_url, created_at, updated_at`. Trả `data[]`, `next_cursor`.
- `GET /admin/events/{event_id}` đọc header từ `event`: `id, status, visible, title, description, category, home_team, away_team, league, round, poster_mobile_url, poster_desktop_url, max_tickets_per_order`.
- `EventDetail.seat_layout_id` là `event.seat_map_id`. `sku_count` là số dòng `sku` của event. `showtimes[]`, `price_tiers[]`, `ticket_types[]`, `zone_prices[]` được truy vấn riêng.
- `showtimes[]` đọc `showtime.id, event_id, seat_map_id, cancelled_at, starts_at, ends_at, sale_opens_at, sale_closes_at`. API đổi `seat_map_id` thành `seat_layout_id`. `status` tính theo trạng thái event, thời điểm hủy, các mốc giờ và thời điểm đọc. `venue.id/name/address/city` đến từ `showtime.seat_map_id → seat_map.venue_id → venue`; `venue.city_name` do mapper suy ra.

[SQL event và suất](../../../backend/vingo-backend/src/main/java/vingo/catalog/internal/EventRepository.java) · [Mapper Admin](../../../backend/vingo-backend/src/main/java/vingo/catalog/web/AdminVenueController.java)

### PriceTier, SkuPrice, ZonePriceInput, ZoneSkuPreview

| Field JSON | Nguồn |
|---|---|
| `price_tiers[].id`, `price_tiers[].ticket_type_id` | Cùng là `ticket_type.id`. Đây là ID phải dùng cho `tier_id` khi thêm giá. |
| `price_tiers[].name`, `price_tiers[].color` | `ticket_type.name`, `ticket_type.color` |
| `price_tiers[].price` | `ticket_type_price.price` có hiệu lực gần nhất tại lúc đọc. Nếu chưa có giá hiệu lực, lấy dòng có hiệu lực sớm nhất; thiếu toàn bộ lịch sử thì trả 0. |
| `price_tiers[].prices[]` | `ticket_type_price.id, ticket_type_id, price, effective_from, created_by, reason, created_at`. Mapper hiện gán alias `sku_id` bằng ID hạng trong response giá. |
| `ticket_types[]` | Cùng danh sách với `price_tiers[]` trong `EventDetail` |
| `zone_prices[].zone_id` | `event_zone.zone_id` |
| `zone_prices[].ticket_type_id`, `zone_prices[].price_tier_id` | Cùng là `event_zone.ticket_type_id` |
| `zone_prices[].display_name` | `event_zone.display_name` |
| `ZoneSkuPreview.event_id` | ID event được preview |
| `ZoneSkuPreview.zones[]` | `zone_id`, `zone_name`, `kind`, `ticket_type_id`, `ticket_type_name`, `price`, `sku_count`. Tên khu ưu tiên `display_name` trong request, sau đó dùng `zone.name`. Hạng và giá đến từ `ticket_type` và `ticket_type_price`. |
| `total_sku_count`, `seated_sku_count`, `standing_sku_count` | Số SKU dự tính. Khu ngồi đếm ghế thuộc khu; khu đứng tính một SKU mỗi khu được gán. |

`ticket_type.id` là ID hạng, `ticket_type_price.id` là ID phiên bản giá, và `sku.id` là ID lựa chọn bán theo ghế/khu. [SQL giá](../../../backend/vingo-backend/src/main/java/vingo/catalog/internal/EventPricingRepository.java) · [Logic preview](../../../backend/vingo-backend/src/main/java/vingo/catalog/EventPricingService.java)

### Layout, khu và tồn kho

| Response | Nguồn |
|---|---|
| `SeatLayout` | `seat_map.id, venue_id, venue_map_id → overview_id, layout_type, name, version, created_at`. `zone_count` và `seat_count` được đếm từ `zone`, `seat`. |
| `SeatLayoutOverview` | Các field trên, thêm `seat_map.view_box` và `zones[]`: `zone.id, code, name → label, kind, capacity`. API không có field `status`; layout nháp ở phiên import. |
| `SeatLayoutZoneDetail` | Header khu và, nếu là khu ngồi, `rows[].row_label`, `seats[].id/number/x/y`. |
| `SeatMap` | ID và trạng thái suất; khu từ `zone` và tên tùy chỉnh từ `event_zone`; ghế từ `seat`; hạng và giá từ `sku`, `ticket_type`, `ticket_type_price`. Trạng thái ghế lấy từ `seat_inventory.status/held_until`; số chỗ đứng còn lại từ `standing_inventory.capacity - sold - held`. |

[SQL layout](../../../backend/vingo-backend/src/main/java/vingo/catalog/internal/SeatLayoutRepository.java) · [SQL seat map](../../../backend/vingo-backend/src/main/java/vingo/search/internal/SeatmapRepository.java)

### Đơn, vé, check-in, audit và báo cáo

| API | Dữ liệu backend và bảng chính |
|---|---|
| `adminListOrders`, `adminGetOrder` | `ticket_order.id, code, status, showtime_id, total_amount, expires_at, paid_at, buyer_name, buyer_email, buyer_phone, created_at`. Join `showtime/event/seat_map/venue` cho thông tin bối cảnh. `order_seat/order_standing` cho SKU, hạng, đơn giá và số lượng; `seat/zone/event_zone` cho tên khu, hàng và số ghế. `review_reason` lấy từ `order_status_history`. |
| `adminGetOrder` thêm payment và lịch sử | `payment.id, order_id, txn_ref, status, expires_at`; `amount` từ `ticket_order.total_amount`; `last_response_code` từ `payment_gateway_log.response_code`. Lịch sử lấy `order_status_history.to_status, at` và thông tin tác nhân từ `user_account`. |
| `adminListTickets` | `ticket.id, code, status, used_at, showtime_id` và các khóa liên kết; `qr_version` dùng trong nghiệp vụ. Join đơn, event, showtime, venue, zone và seat. SKU, hạng, giá vé lấy theo dòng đơn đã chốt. |
| `listShowtimeCheckins` | `ticket_scan.result, scanned_at`, `ticket.code, used_at`, `seat.row_label, number`, `zone.name` hoặc `event_zone.display_name`. JSON gồm `result, ticket_code, zone, row, number, scanned_at, first_used_at`. |
| `listAuditLogs` | `audit_log.id, actor_id, action, entity, entity_id, reason, via_agent, at, before, after`. `actor_name` từ `user_account.full_name`. |
| `queryMetric` | Đơn PAID và các dòng `order_seat/order_standing` cho doanh thu và vé bán; `seat_inventory/standing_inventory` cho lấp đầy; `ticket_scan` cho check-in. Response có `metric, unit, filters, computed_at, value` hoặc `series[]`, và `definition`. |
| `getAdminDashboard` | Dùng các nguồn metric; đếm user mới từ `user_account.created_at`. Join event, showtime và venue cho suất sắp tới và thống kê theo sân. Response có `from, to, computed_at, timezone, current, previous, on_sale_showtimes, revenue, tickets, tiers, upcoming, venues`. |

[SQL đơn](../../../backend/vingo-backend/src/main/java/vingo/booking/internal/OrderReader.java) · [SQL vé và check-in](../../../backend/vingo-backend/src/main/java/vingo/ticketing/internal/TicketingReader.java) · [SQL audit](../../../backend/vingo-backend/src/main/java/vingo/adminops/internal/AuditLogRepository.java) · [Nguồn metric](../../../backend/vingo-backend/src/main/java/vingo/reporting/internal/MetricSources.java)

### API quản trị ngoài danh mục tool V2.3

| Operation hoặc nhóm API | Field dữ liệu chính |
|---|---|
| `deleteVenue` | Trả 204. Kiểm các suất chưa kết thúc bằng quan hệ `showtime → seat_map → venue`, rồi xóa mềm bằng `venue.deleted_at`. |
| `listVenueImports`, `getVenueImport`, các bước create/update/upload/delete file/preview/cancel | `venue_import_session.id, owner_id, venue_id, draft, created_at, updated_at, revision, status, result`. File thuộc `venue_import_file`; response file gồm `id, original_filename, role, validation_status, size_bytes, checksum, schema_version, sanitized_svg, issues` theo bước. |
| `listVenueOverviews`, `getVenueOverview` | `venue_map.id, venue_id, version, svg, created_at`. Schema có `svg_content, view_box, gates, areas` cho dữ liệu sơ đồ. |
| `getSeatLayoutSvg`, `exportSeatLayoutZones`, `exportSeatLayoutSeats` | SVG từ `seat_map.svg`. CSV khu đọc `zone.code, name, kind, capacity`; CSV ghế đọc mã khu, `seat.row_label` và `seat.number`. |
| `adminListUsers`, `adminGetUser`, `exportAdminUsers`, `adminUserStatistics`, `adminUpdateUser` | Nguồn `user_account`: `id, email, phone, avatar_url, full_name, role, email_verified_at, phone_verified_at, admin_locked, locked_until, deleted_at, created_at`. `status` và boolean `has_password` được suy ra. Statistics trả `total, active, locked, unverified, deleted`. |
| `resolveReview`, `reconcileOrder`, `voidTicket`, `reissueTicketQr` | Nghiệp vụ xử lý đơn, giao dịch hoặc vé. Tra field đơn/vé từ các read model ở mục trên, và schema response riêng của từng operation trong OpenAPI. |
| `suggestVenueLocations`, `reverseGeocodeVenueLocation` | Kết quả tra cứu địa chỉ có `label, address, lat, lng, city, district`. Các field được lưu vào `venue` khi tạo hoặc sửa sân. |

[Import repository](../../../backend/vingo-backend/src/main/java/vingo/catalog/internal/VenueImportRepository.java) · [Admin accounts](../../../backend/vingo-backend/src/main/java/vingo/auth/internal/AdminAccountRepository.java)

## 4. Snapshot và retry của KAN-202

| Operation | Snapshot khi đề xuất | Sau timeout hoặc 5xx |
|---|---|---|
| `UPDATE_VENUE` | `getAdminVenue`, giữ đúng các field trong PATCH. So mã `city` đã chuẩn hóa khi đọc lại. | Đọc lại. Bằng giá trị mới → `SUCCEEDED`. Bằng `before` → gửi lại một lần. Trường hợp còn lại → `UNKNOWN`. |
| `UPDATE_EVENT` | Header `getAdminEvent`, giữ đúng các field trong PATCH. Dùng trạng thái và các suất để kiểm điều kiện đổi layout. | Quy tắc đọc lại của PATCH. |
| `UPDATE_SHOWTIME` | Tìm đúng suất trong `getAdminEvent.showtimes[]`. Khi sửa giờ, kiểm thứ tự và chụp cả bốn mốc. | Quy tắc đọc lại của PATCH. So timestamp theo cùng thời điểm, kể cả offset khác nhau. |
| `UPDATE_PRICE_TIERS` | `getAdminEvent.price_tiers[]`, lấy `id, name, price, color`. Thẻ hiện thêm/sửa/xóa. | Đọc lại cả danh sách, đối chiếu hạng mới theo tên khi chưa có ID. Gửi lại cùng PUT nếu danh sách vẫn bằng `before`. |
| `ADD_TIER_PRICE` | Kiểm event chưa `CANCELLED` và `tier_id` thuộc event. Chụp giá hiệu lực và ID các phiên bản giá hiện có. | `UNKNOWN` để admin kiểm tra lịch sử giá. |
| `UPDATE_ZONE_PRICES` | `getAdminEvent.zone_prices[]`, chuẩn hóa ID hạng và thứ tự theo `zone_id`. Preview trong cùng turn cho đúng event, layout và bộ assignments mới. | Đọc lại toàn bộ danh sách. Gửi lại cùng PUT nếu layout và danh sách vẫn khớp snapshot. |

Trước lần ghi đầu cho các thao tác sửa có snapshot, executor đọc lại và so giá trị hiện tại với `before`. Lệch thì trả `FAILED / STALE_PROPOSAL` với 0 request ghi. `before` và `display.changes` dùng trong agent; body gửi backend là dữ liệu nghiệp vụ đã xác nhận.

[V2.3 mục 4.4 và 7](VINGO_AGENT_DESIGN_V2.3.md) · [Executor hiện tại](../src/vingo_agent/actions/execution.py)
