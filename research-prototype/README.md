# 研究原型腳本（2026-09-11）

這些是 `spec-signal-research-rework.md` Phase 1 的藍本，不是生產程式碼。
它們產生了 spec 第 0 節引用的全部數據。

執行順序（需要 pandas / numpy，以及一個能連到 postgres 的環境）：

1. 先從資料庫匯出 10 秒分桶的 CSV（見 build.py 開頭註解的 SQL）
2. `build.py`    — 重建特徵面板，並對照 app.scoring / app.trade_signal 驗證保真度
3. `analyze.py`  — 分數分布、前瞻報酬響應曲線、三重障礙回測與門檻掃描
4. `analyze2.py` — 多空拆分、市場基準、障礙尺寸、波動 IC
5. `analyze3.py` — symbol-demean 的擇時 alpha、被動對照組
6. `analyze4.py` — within-symbol 波動 IC、共用區塊的方向性 alpha

已知的方法論陷阱（Phase 1 必須保留這些修正）：

- pooled rank IC 會被跨幣波動差異汙染：pooled −0.049 vs within-symbol +0.017，結論相反
- 每個子集各自挑選非重疊區塊會產生互相矛盾的結果：
  analyze3 出現 60m 合計 +22.1 bps (t=+2.48)，改用共用區塊後變成 −3.3 bps (t=−1.26)
- 樣本期間為單邊下跌（43 檔中 39 檔跌，中位數 −6.02%），未 demean 會把 beta 誤認為 alpha
