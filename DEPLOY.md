# 部署到 Vercel + Supabase

## 架構說明

```
GitHub Actions（美股收盤後，週一～五 UTC 22:00）
    → 從 Wikipedia + yfinance 抓 S&P 500 市值
    → 寫入 Supabase PostgreSQL

Vercel（Serverless）
    → 讀取 Supabase
    → 回傳給瀏覽器
```

---

## 步驟一：建立 Supabase 資料庫

1. 前往 https://supabase.com → **Start your project**（免費）
2. 建立新 Project（Region 選 **Northeast Asia (Tokyo)**）
3. 記下 **Database Password**
4. 進入 Project → **Settings → Database → Connection string → URI**
5. 複製連線字串（把 `[YOUR-PASSWORD]` 換成你的密碼）：
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxx.supabase.co:5432/postgres
   ```

---

## 步驟二：上傳到 GitHub

1. 前往 https://github.com/new 建立新 Repository（可設 Private）
2. 在本機執行：

```powershell
cd e:\python練習\us-market-cap

git init
git add .
git commit -m "init: US market cap screener"
git branch -M main
git remote add origin https://github.com/你的帳號/倉庫名稱.git
git push -u origin main
```

3. GitHub Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DATABASE_URL`
   - Value: 你的 Supabase 連線字串

---

## 步驟三：部署到 Vercel

1. 前往 https://vercel.com → **Add New Project**
2. **Import Git Repository** → 選剛建立的 GitHub Repo
3. **Framework Preset** 選 `Other`
4. 展開 **Environment Variables**，新增：
   - Key: `DATABASE_URL`
   - Value: 你的 Supabase 連線字串
5. 點 **Deploy**

完成後會得到網址，例如 `your-project.vercel.app`

---

## 步驟四：手動觸發第一次資料抓取

GitHub Repo → **Actions → Daily US Market Cap Update → Run workflow**

首次執行約需 2–3 分鐘（抓取 500 檔股票資料）。

---

## 每日自動更新

GitHub Actions 設定在每週一到週五 **UTC 22:00（美東時間下午 4–6 點，收盤後）** 自動執行。

如需手動更新：GitHub → Actions → Daily US Market Cap Update → Run workflow

---

## 本地開發（SQLite）

```powershell
cd e:\python練習\us-market-cap
python app.py          # 不需要 DATABASE_URL，使用本地 SQLite
# 開啟 http://localhost:5001
```

---

## 檔案結構

```
us-market-cap/
├── api/index.py                    # Vercel serverless 入口
├── app.py                          # Flask 路由（自動切換 DB）
├── database.py                     # SQLite（本地開發）
├── database_pg.py                  # PostgreSQL（Vercel + Supabase）
├── fetcher.py                      # yfinance 資料抓取
├── daily_fetch.py                  # GitHub Actions 執行腳本
├── vercel.json                     # Vercel 設定
├── requirements.txt
├── templates/index.html            # 前端頁面
└── .github/workflows/
    └── daily_update.yml            # 每日自動更新
```
