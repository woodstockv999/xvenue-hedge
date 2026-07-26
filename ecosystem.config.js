// xvenue-hedge: 依存が揃った hyperliquid-bot venv を interpreter に使う(dep二重管理を避ける)。
// ★2026-07-26: perpl層の参照先を hlbot-sandbox → apps/hyperliquid-bot へ移したのに合わせて
//   interpreter も揃えた(main.py の sys.path.insert のコメント参照)。必要な依存
//   (httpx/websocket/nacl/yaml/dotenv/eth_account/msgpack/requests)は両venvに揃っていることを実測確認済み。
module.exports = {
  apps: [{
    name: "xvenue-hedge",
    script: "main.py",
    interpreter: "/home/w00dst0ck/apps/hyperliquid-bot/.venv/bin/python",
    cwd: "/home/w00dst0ck/apps/xvenue-hedge",
    autorestart: true,
    // min_uptime / restart_delay 未設定だと、perpl/txflow API の一時障害で起動が連続失敗した際に
    // max_restarts:20 を数十秒で使い切り、PM2 が再起動を諦めて **両会場に建玉を残したまま停止** する。
    // hyperliquid-bot 側は 2026-07-06 の実事故を受けて既に対策済みだったが、xvenue には
    // 横展開されていなかった(2026-07-26 監査で検出)。同じ値に揃える。
    restart_delay: 5000,
    min_uptime: "60s",
    max_restarts: 20,
    env: { PYTHONUNBUFFERED: "1" },
  }],
};
