#!/usr/bin/env python3
"""lead 名目サイズの A/B 集計(2026-07-29)。**目的関数は出来高/時**。

## なぜこの A/B が要るか
$139(2脚) → $200(1脚) でセルの出来高は 1.5倍になったが、内訳を見ると
**lead の maker 約定率が 57% → 32% に落ちていた**。出来高が増えたのは
ETH 試行 45s と hold 90s が消えて1サイクルが短くなったからで、
サイズを上げた効果ではない。

同じ弾性が続くなら $270 では約定率 20% まで落ち、出来高はむしろ減る。
つまり**最適サイズは現行の内側にあり得る**。$139 と $200 は構造が違う
(ETH 脚の有無)ので弾性の推定に使えない → 同一構造・同一時間帯で並走させる。

## 判定指標 = 出来高/時（効率でも約定率でもない）
- 約定率だけ見ると「小さいほうが良い」に決まっている(必ず小が勝つ)
- 効率(出来高÷損失)はサイズにほぼ中立(出来高も損失も名目に比例)なので差が出にくい
- 実際に増やしたいのは**単位時間あたりの出来高** = 約定率 × サイズ × 試行レート

## 試行数の数え方
台帳には**完走したサイクルしか載らない**ので、約定率の分母は台帳から作れない。
見送りは pm2 ログの `cycle見送り(ab=$…)` 行から腕別に数える。
★腕は attempts(見送り込み)で振っているので、両腕の試行数はほぼ等しくなるはず。
  大きく偏っていたら `self.cycles` で振る実装に戻っていないか疑うこと。
"""
import json
import re
import statistics as st
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CYCLES = APP / "data" / "cycles.jsonl"
LOG = Path.home() / ".pm2" / "logs" / "xvenue-hedge-out.log"
_SKIP_RE = re.compile(r"cycle見送り\(ab=\$(\d+) arms=([0-9/]*)\)")


def load_rows(hours: float | None) -> list[dict]:
    rows = []
    for line in CYCLES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dry_run") or "lead_notional_ab" not in r:
            continue
        rows.append(r)
    # ★最新の「腕の組」の行だけ使う。腕を差し替えた前後を混ぜると、残した腕にだけ
    #   相手不在の時間の行が乗って静かに有利になる([170,230]→[170,200] で実際に発生)。
    if rows:
        latest = rows[-1].get("lead_notional_ab_arms", "")
        rows = [r for r in rows if r.get("lead_notional_ab_arms", "") == latest]
    if hours and rows:
        newest = max(r["ts"] for r in rows)
        rows = [r for r in rows if r["ts"] >= newest - hours * 3600]
    return rows


def skip_counts() -> dict:
    """見送りを腕別に数える。ログはローテするので、取りこぼしは n の偏りとして現れる。"""
    out = defaultdict(int)
    try:
        text = LOG.read_text(errors="replace")
    except Exception:
        return out
    for m in _SKIP_RE.finditer(text):
        out[float(m.group(1))] += 1
    return out


def main() -> None:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = load_rows(hours)
    if not rows:
        print("A/B 行が無い(config の lead_notional_ab を確認)")
        return
    skips = skip_counts()
    arms = defaultdict(list)
    for r in rows:
        arms[float(r["lead_notional_ab"])].append(r)

    span_h = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 3600 or 1e-9
    print(f"# lead 名目 A/B  n={len(rows)} 完走 / 窓 {span_h*60:.0f}分"
          + (f" / 直近 {hours}h" if hours else ""))
    print()
    print("| 腕 | 完走 | 見送り | 約定率 | 実名目 | 出来高/h | 価格bps | 効率 |")
    print("|---|---|---|---|---|---|---|---|")
    best, best_v = None, -1.0
    for arm in sorted(arms):
        v = arms[arm]
        nfill, nskip = len(v), int(skips.get(arm, 0))
        rate = nfill / (nfill + nskip) if (nfill + nskip) else 0.0
        vol = sum(x["volume_usd"] for x in v)
        net = sum(x["net_usd"] for x in v)
        # 腕は交互なので、この腕が単独で走ったときの出来高は窓あたりの2倍
        vph = vol / span_h * 2
        act = st.median([x.get("lead_notional_usd", arm) for x in v])
        b = [x["legs"]["perpl:BTC"]["pnl"] / x["legs"]["perpl:BTC"]["notional"] * 1e4
             for x in v if "perpl:BTC" in x.get("legs", {})]
        bps = st.mean(b) if b else float("nan")
        eff = f"{vol/-net:,.0f}" if net < 0 else "黒字"
        print(f"| ${arm:,.0f} | {nfill} | {nskip} | {rate*100:.0f}% | ${act:,.0f} | "
              f"${vph:,.0f} | {bps:+.2f} | {eff} |")
        if vph > best_v:
            best, best_v = arm, vph
    print()
    print("★『この腕が単独で走ったときの出来高/h』= 窓内の腕別出来高 × 2(腕は交互のため)。")
    print("  試行時間が腕でほぼ同じ(見送りは同じ timeout を使う)ことを前提にした近似。")
    if len(arms) >= 2:
        print(f"\n暫定優位: **${best:,.0f}**（出来高 ${best_v:,.0f}/h）")
        print("★片腕 n≥40 まで待つこと。約定率は時間帯で振れるので少数では逆に出る"
              "([[feedback-measure-the-right-unit-2026-07-28]])。")


if __name__ == "__main__":
    main()
