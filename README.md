# AI Affiliate Video Maker (MVP)
Butuh: Python 3.10+, FFmpeg terpasang.

    cd backend
    pip install -r requirements.txt
    export OPENAI_API_KEY=sk-...        # opsional model: IMAGE_MODEL
    uvicorn main:app --reload
Buka http://localhost:8000

Alur: Pengaturan (edit prompt) -> Template (upload MP4 + area x,y,w,h + rentang detik) -> Produk (upload, Generate foto AI, Gunakan foto) -> pilih produk (boleh banyak = batch) -> Render -> Riwayat -> Unduh.

## Deploy (Render / Railway)
1. Upload folder ini ke repo GitHub.
2. Buat Web Service dari repo, pilih runtime Docker (Dockerfile ada di root).
3. Environment variables: OPENAI_API_KEY, APP_PASSWORD (password login, username bebas).
4. Tambahkan persistent disk/volume dengan mount path /data (tanpa ini video dan database hilang saat restart).
5. Buka link publik dari layanan tersebut.
