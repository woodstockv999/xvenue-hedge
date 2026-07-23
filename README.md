# xvenue-hedge

txflow BTC × perpl BTC クロス会場デルタ中立farm。txflowでBTCをmaker farm(将来pt)しつつ
perplで逆BTCをヘッジ=デルタ中立・両会場で二重farm。効率値(出来高÷損失)は `data/cycles.jsonl`
の独立台帳で計測。設計・背景は memory `txflow-perpl-xhedge-farm-2026-07-23`。

- 実行: `pm2 start ecosystem.config.js`(interpreter=hlbot-sandbox venv=両会場の依存済)
- 鍵: txflow=txflow-bot/.env、perpl=hyperliquid-bot/.env から読む(本repoに秘密は置かない)
- クライアントは保守版を import 再利用(txflow=別名パッケージ、perpl=hlbot-sandbox src)
- **dry_run既定**。実弾化は config.yaml `dry_run:false`(dry_runで1サイクル完走確認後)
- perpl は共有IPのCF 1015レート制限あり=pair_hedge(perpl SOL/ETH farm)と帯域競合。poll控えめ。
