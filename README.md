# Vẽ Tay cho Windows

Tải `VeTay-Windows-Setup.exe` từ [GitHub Releases](https://github.com/taikhoanzaizai-ux/vetay-muse/releases) sau khi build thành công. Bộ cài chứa Python, các thư viện, FFmpeg và Whiteboard Animation VI kèm model ISNet. Bản cài không cần bản Whiteboard Animation VI trong Downloads, Git hay lệnh PowerShell khi sử dụng. Giao diện mở bằng cửa sổ ứng dụng WebView2. Chế độ Flow mở Microsoft Edge với phiên đăng nhập riêng.

## Tạo video

1. Mở biểu tượng Vẽ Tay. Nhập kịch bản hoặc tải WAV/MP3. Với kịch bản, Edge TTS tạo lời đọc tiếng Việt. Whisper tải model nhận dạng ở lần đầu và chia cảnh theo mốc lời đọc.
2. Chọn Google Flow nếu muốn dùng ảnh do Flow tạo. Dán link dự án, đăng nhập Google trong Edge và chọn chế độ ảnh cùng Nano Banana 2.1. Tích hợp Flow hiện thử nghiệm và chưa được xác minh trên tài khoản thực. Nếu trình tự Flow thay đổi, có thể dùng chế độ tải ảnh có sẵn.
3. Bộ Whiteboard Animation VI dựng cảnh ngay trong phần mềm, sau đó ghép voice và xuất MP4 dọc 1080 × 1920. Video được lưu trong `%LOCALAPPDATA%\VeTay`. App chưa tự đưa video lên Canva.

## Cập nhật

Nút **Cập nhật qua GitHub** tìm bộ cài mới hơn ở Releases. App tải bộ cài đúng repo, kiểm tra kích thước và SHA-256 do GitHub cung cấp rồi mở trình cài đặt để nâng cấp.

## Build

Workflow `Windows standalone installer` chạy `Build_Windows.ps1` và phát hành bộ cài. Repo upstream: [Whiteboard Animation VI](https://github.com/letuandat131/whiteboard-animation-vi). Bản build FFmpeg và thông tin mã nguồn: [Gyan FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/). Bộ cài giữ nội dung thông báo và giấy phép của các thành phần đi kèm.
