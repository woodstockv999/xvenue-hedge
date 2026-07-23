// xvenue-hedge: 依存が揃った hlbot-sandbox venv を interpreter に使う(dep二重管理を避ける)。
module.exports = {
  apps: [{
    name: "xvenue-hedge",
    script: "main.py",
    interpreter: "/home/w00dst0ck/hlbot-sandbox/.venv/bin/python3",
    cwd: "/home/w00dst0ck/apps/xvenue-hedge",
    autorestart: true,
    max_restarts: 20,
    env: { PYTHONUNBUFFERED: "1" },
  }],
};
