"""Asynchronous Telegram notification client and interactive command handler."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from config.settings import Settings, settings

logger = logging.getLogger(__name__)

_PLACEHOLDER_VALUES = {
    "",
    "your_telegram_bot_token_here",
    "your_telegram_chat_id_here",
}


def _clean_md(text: str) -> str:
    """Remove characters that can break legacy Telegram Markdown entity parsing."""
    return str(text or "").replace("_", "-").replace("*", "").replace("`", "'").replace("[", "(").replace("]", ")")


class TelegramNotifier:
    """Non-blocking Telegram notifier and two-way command polling client."""

    def __init__(
        self,
        custom_settings: Optional[Settings] = None,
        db: Optional[Any] = None,
        risk_engine: Optional[Any] = None,
    ) -> None:
        self.cfg = custom_settings or settings
        self.db = db
        self.risk_engine = risk_engine

        self.token = (self.cfg.TELEGRAM_BOT_TOKEN or "").strip()
        self.chat_id = (self.cfg.TELEGRAM_CHAT_ID or "").strip()
        self.enabled = (
            self.token not in _PLACEHOLDER_VALUES
            and self.chat_id not in _PLACEHOLDER_VALUES
        )

        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id: int = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def register_commands_menu(self) -> None:
        """Register clickable command menu in Telegram client UI."""
        if not self.enabled:
            return
        try:
            client = await self._get_client()
            await client.post(
                f"{self.base_url}/setMyCommands",
                json={
                    "commands": [
                        {"command": "durum", "description": "Kasa bakiyesi (Spot/Futures), aktif mod ve özet"},
                        {"command": "liste", "description": "İzleme listesi, RSI ve güven puanları"},
                        {"command": "pozisyonlar", "description": "Açık işlem ve emirler"},
                        {"command": "dersler", "description": "Öğrenilen hatalar ve aktif güven cezaları"},
                        {"command": "audit", "description": "Son 24 saatlik Brier kalibrasyonu"},
                        {"command": "panic", "description": "Acil Durum: Her şeyi kapat ve kilitle"},
                        {"command": "baslat", "description": "Durdurulmuş sistemi tekrar aktif et"},
                        {"command": "yardim", "description": "Komut listesini göster"},
                    ]
                },
            )
        except Exception as exc:
            logger.debug("Could not register Telegram command menu: %s", exc)

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send notification message to configured chat_id safely, falling back to plain text on HTTP 400."""
        if not self.enabled:
            return False
        try:
            client = await self._get_client()
            payload: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            resp = await client.post(f"{self.base_url}/sendMessage", json=payload)
            if resp.status_code == 200:
                return True

            # Fallback to plain text if Markdown entity parsing fails (HTTP 400)
            if resp.status_code == 400 and parse_mode:
                plain_text = text.replace("*", "").replace("`", "").replace("_", "")
                resp_fallback = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": plain_text,
                        "disable_web_page_preview": True,
                    },
                )
                if resp_fallback.status_code == 200:
                    return True

            logger.warning("Telegram sendMessage returned %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Telegram notification failed (ignored): %s", exc)
        return False

    async def notify_entry(
        self,
        symbol: str,
        pos_id: str,
        entry_price: float,
        qty: float,
        cost_usd: float,
        invalidation_price: float,
        take_profit_price: float,
        confidence: float,
        regime: str,
    ) -> None:
        sl_pct = ((invalidation_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
        tp_pct = ((take_profit_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
        msg = (
            f"🟢 *YENİ ALIM YAPILDI (SPOT LONG)*\n\n"
            f"🪙 *Parite:* `{symbol}`\n"
            f"💰 *Giriş Fiyatı:* `${entry_price:,.4f}`\n"
            f"📦 *Miktar:* `{qty:.6f}` (~`${cost_usd:,.2f}`)\n"
            f"🎯 *Kâr Al (TP):* `${take_profit_price:,.4f}` ({tp_pct:+.2f}%)\n"
            f"🛡️ *Sunucu SL:* `${invalidation_price:,.4f}` ({sl_pct:.2f}%)\n"
            f"📊 *Güven / Rejim:* `%{confidence * 100:.1f}` | `{regime.upper()}`\n"
            f"🆔 *Pozisyon ID:* `{pos_id}`"
        )
        await self.send_message(msg)

    async def notify_exit(
        self,
        symbol: str,
        pos_id: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        realized_pnl: float,
        reason: str,
    ) -> None:
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
        icon = "🎉" if realized_pnl >= 0 else "🛑"
        color_bar = "🟢" if realized_pnl >= 0 else "🔴"
        msg = (
            f"{color_bar} *POZİSYON KAPANDI* {icon}\n\n"
            f"🪙 *Parite:* `{symbol}`\n"
            f"🚪 *Çıkış Fiyatı:* `${exit_price:,.4f}` (Giriş: `${entry_price:,.4f}`)\n"
            f"📈 *Net K/Z:* `${realized_pnl:+,.2f}` ({pnl_pct:+.2f}%)\n"
            f"📝 *Kapanış Nedeni:* `{reason}`\n"
            f"🆔 *Pozisyon ID:* `{pos_id}`"
        )
        await self.send_message(msg)

    async def notify_system_event(self, title: str, details: str, is_warning: bool = False) -> None:
        icon = "⚠️" if is_warning else "ℹ️"
        msg = f"{icon} *{_clean_md(title).upper()}*\n\n{details}"
        await self.send_message(msg)

    # === TWO-WAY TELEGRAM COMMAND HANDLER ===
    async def start_polling(self) -> None:
        """Start background polling task for incoming commands from operator."""
        if not self.enabled or self._running:
            return
        await self.register_commands_menu()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram_poll_loop")
        logger.info("Telegram command polling loop started.")

    async def stop(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        logger.info("Telegram command polling stopped.")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                client = await self._get_client()
                resp = await client.get(
                    f"{self.base_url}/getUpdates",
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 5,
                        "allowed_updates": ["message"],
                    },
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    updates = data.get("result") or []
                    for upd in updates:
                        self._last_update_id = max(self._last_update_id, int(upd.get("update_id", 0)))
                        msg = upd.get("message") or {}
                        sender_id = str(msg.get("from", {}).get("id") or "")
                        # Hard security check: strictly ignore commands from any unauthorized chat ID
                        if sender_id != self.chat_id:
                            continue
                        text = str(msg.get("text") or "").strip()
                        if text.startswith("/"):
                            await self._handle_command(text)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Telegram poll loop transient error: %s", exc)
                await asyncio.sleep(3.0)
            await asyncio.sleep(1.0)

    async def _handle_command(self, raw_cmd: str) -> None:
        cmd = raw_cmd.split()[0].split("@")[0].lower()

        if cmd in ("/start", "/help", "/yardim", "/yardım"):
            msg = (
                "🤖 *QuantBot Live v3.1 Komut Listesi*\n\n"
                "• `/durum` veya `/status` — Kasa bakiyesi (Spot/Futures), aktif mod ve özet\n"
                "• `/liste` veya `/watchlist` — İzleme listesi, RSI ve güven puanları\n"
                "• `/pozisyonlar` veya `/pos` — Açık işlem ve emirler\n"
                "• `/dersler` veya `/lessons` — Öğrenilen hatalar ve aktif kurallar\n"
                "• `/audit` veya `/brier` — Son 24 saatlik Brier kalibrasyonu\n"
                "• `/saglik` — Sistem sağlığı, clock drift ve hata sayaçları\n"
                "• `/panic` veya `/stop` — 🚨 Acil Durum: Her şeyi kapat ve sistemi kilitle\n"
                "• `/baslat` veya `/resume` — Durdurulmuş sistemi tekrar aktif et"
            )
            await self.send_message(msg)

        elif cmd in ("/saglik", "/sağlık", "/health"):
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            state = self.db.get_all_system_state()
            safe_mode = self.db.is_safe_mode()
            entries_blocked = state.get("entries_blocked", "false")
            drift_ms = state.get("clock_drift_ms", "?")
            resolver_errs = state.get("resolver_errors", "0")
            hb_ts = float(state.get("last_heartbeat_ts", "0") or 0)
            hb_age = f"{int(time.time() - hb_ts)}s" if hb_ts > 0 else "N/A"
            open_pos = len(self.db.get_open_positions(include_unprotected=True))
            split_warn = state.get("wallet_split_warning", "false")
            msg = (
                "🩺 *Sistem Sağlığı (/saglik)*\n"
                f"• Safe Mode: `{'ON' if safe_mode else 'OFF'}`\n"
                f"• Entries Blocked: `{entries_blocked}`\n"
                f"• Clock Drift: `{drift_ms} ms`\n"
                f"• Heartbeat Yaşı: `{hb_age}`\n"
                f"• Resolver Hataları: `{resolver_errs}`\n"
                f"• Açık Pozisyon: `{open_pos}`\n"
                f"• Sermaye Bölünmüş Uyarısı: `{split_warn}`"
            )
            await self.send_message(msg)

        elif cmd == "/onayla_ogrenme":
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            from engine.learning_budget import LearningBudgetManager
            eq = float(self.db.get_state("total_equity_usd", "0.0") or 0.0)
            LearningBudgetManager(db=self.db, custom_settings=self.cfg).approve_budget_renewal(eq)
            await self.send_message(f"✅ 14 günlük canlı öğrenme bütçesi yenilendi (referans bakiye: `${eq:.2f}`).")

        elif cmd == "/ogrendim":
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            rows = self.db._conn.execute(
                "SELECT strategy_id, state, g1_passed FROM strategy_lifecycle ORDER BY strategy_id"
            ).fetchall()
            susp = self.db._conn.execute("SELECT count(*) FROM suspended_patterns").fetchone()
            susp_cnt = int(susp[0] or 0) if susp else 0
            lines = ["🧠 *Öğrenme Özeti (/ogrendim)*"]
            for r in rows:
                lines.append(f"• `{r[0]}`: `{r[1]}` (G1=`{r[2]}`)")
            lines.append(f"• Askıya alınan desen sayısı: `{susp_cnt}`")
            await self.send_message("\n".join(lines))

        elif cmd in ("/durum", "/status", "/ozet", "/özet"):
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            state = self.db.get_all_system_state()
            eq = float(state.get("total_equity_usd", "0.0") or 0.0)
            spot_free = float(state.get("spot_free_usdt") or state.get("free_usdt", "0.0") or 0.0)
            fut_free = float(state.get("futures_free_usdt", "0.0") or 0.0)
            fut_total = float(state.get("futures_total_usdt", str(fut_free)) or fut_free)
            fut_used = max(0.0, fut_total - fut_free)
            active_mode_raw = state.get("active_trading_mode", "SPOT").upper()
            fut_lev = int(getattr(self.cfg, "FUTURES_DEFAULT_LEVERAGE", 20))
            is_paper = getattr(self.cfg, "is_paper", True)
            env_badge = "🧪 PAPER" if is_paper else "🔴 LIVE"
            if is_paper:
                mode_badge = f"[{env_badge}] {active_mode_raw} (Sanal Bakiye)"
            elif active_mode_raw.startswith("FUTURE") or fut_free >= 3.0:
                mode_badge = f"[{env_badge}] ⚡ FUTURES {fut_lev}x (ISOLATED)"
            elif fut_total >= 5.0:
                mode_badge = f"[{env_badge}] ⚡ FUTURES {fut_lev}x AKTİF (Teminat Dolu -> Yeni Giriş: SPOT)"
            else:
                mode_badge = f"[{env_badge}] 🪙 SPOT MODE (Min $11.50)"
            pnl = float(state.get("daily_realized_pnl_usd", "0.0") or 0.0)
            dd = float(state.get("current_drawdown_pct", "0.0") or 0.0)
            min_conf = float(state.get("min_confidence_threshold", str(self.cfg.MIN_CONFIDENCE_THRESHOLD)) or 0.68)
            daily_trades = self.db.get_daily_trades_count() if hasattr(self.db, "get_daily_trades_count") else 0
            max_daily_trades = int(getattr(self.cfg, "DAILY_MAX_TRADES", 20))
            open_pos = self.db.get_open_positions() if hasattr(self.db, "get_open_positions") else []
            regime = _clean_md(state.get("macro_regime", "Bilinmiyor").upper())
            commentary = _clean_md(state.get("macro_commentary", "Veri yok"))[:240]
            halted = state.get("system_halted", "false").lower() in ("true", "1")
            status_badge = "🔴 DURDURULDU (HALTED)" if halted else "🟢 AKTİF (ÇALIŞIYOR)"

            # Breakdown of spot coins held in open positions
            spot_coins_list = []
            spot_positions_val = 0.0
            for p in open_pos:
                if str(p.get("market_type", "spot")).lower() == "spot":
                    val = float(p.get("notional_usd") or p.get("cost_usd") or 0.0)
                    sym = p.get("symbol", "").split("/")[0]
                    spot_positions_val += val
                    spot_coins_list.append(f"{sym}: ~${val:.2f}")

            spot_coins_detail = f" ({', '.join(spot_coins_list)})" if spot_coins_list else ""

            msg = (
                f"📊 *SİSTEM DURUM RAPORU*\n\n"
                f"• *Durum:* {status_badge}\n"
                f"• *Aktif Mod:* `{mode_badge}`\n\n"
                f"💰 *CÜZDAN & BAKİYE KIRILIMI:*\n"
                f"  💵 *Spot Boşta (Nakit):* `${spot_free:,.2f} USDT`\n"
                f"  🪙 *Spot Açık Coin:* `${spot_positions_val:,.2f}`{spot_coins_detail}\n"
                f"  ⚡ *Vadeli (Futures):* `${fut_total:,.2f} USDT` `(${fut_free:,.2f} Boşta / ${fut_used:,.2f} Pozisyonda)`\n"
                f"  ━━━━━━━━━━━━━━━━━━━━\n"
                f"  📊 *Toplam Kasa (Net Equity):* `${eq:,.2f}`\n\n"
                f"📈 *İŞLEM & PERFORMANS:*\n"
                f"• *Açık Pozisyon:* `{len(open_pos)} Adet`\n"
                f"• *Günlük İşlem Kotası:* `{daily_trades} / {max_daily_trades}`\n"
                f"• *Min Güven Eşiği:* `%{min_conf * 100:.0f}`\n"
                f"• *Günlük K/Z:* `${pnl:+,.2f}`\n"
                f"• *Drawdown:* `%{dd:.2f}`\n"
                f"• *Piyasa Rejimi:* `{regime}`\n"
                f"• *Saat Farkı:* `{state.get('clock_drift_ms', '0')} ms`\n"
                f"• *Makro Yorum:* {commentary}"
            )
            await self.send_message(msg)

        elif cmd in ("/liste", "/watchlist"):
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            watchlist = self.db.get_watchlist()
            if not watchlist:
                await self.send_message("ℹ️ İzleme listesi şu an boş.")
                return
            lines = [f"📋 *İzleme Listesi ({len(watchlist)} Parite)*\n"]
            for item in watchlist[:10]:
                lines.append(
                    f"• *{item['symbol']}* | Fiyat: `${item['price']:,.4f}` | RSI: `{item.get('rsi_14', 0):.1f}` | Q: `{item.get('setup_quality', 0)}` | Güven: `%{item.get('direction_confidence', 0)*100:.0f}`"
                )
            await self.send_message("\n".join(lines))

        elif cmd in ("/pozisyonlar", "/pozsyonlar", "/pozisyon", "/pos", "/positions"):
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            open_pos = self.db.get_open_positions()
            if not open_pos:
                await self.send_message("ℹ️ Şu anda açık pozisyon bulunmuyor.")
                return
            lines = [f"💼 *Açık Pozisyonlar ({len(open_pos)})*\n"]
            for p in open_pos:
                sl_id = str(p.get("sl_exchange_order_id") or "")
                p_mode = "⚡ FUTURES" if sl_id.startswith("2000") else "🪙 SPOT"
                cur_p = float(p.get("current_price") or p["entry_price"])
                upnl = float(p.get("unrealized_pnl_usd") or 0.0)
                cost = float(p.get("cost_usd") or 0.0)
                lines.append(
                    f"• *{p['symbol']}* ({p_mode} `${cost:,.2f}`) | Giriş: `${float(p['entry_price']):,.4f}` → Güncel: `${cur_p:,.4f}` | K/Z: `${upnl:+,.2f}` | TP: `${float(p['take_profit_price']):,.4f}` | SL: `${float(p['invalidation_price']):,.4f}`"
                )
            await self.send_message("\n".join(lines))

        elif cmd in ("/dersler", "/ders", "/lessons"):
            if not self.db or not hasattr(self.db, "get_trade_lessons"):
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            lessons = self.db.get_trade_lessons(limit=8)
            if not lessons:
                await self.send_message(
                    "📚 *Öğrenilen Kurallar (Mistake Learner)*\n\n"
                    "✅ Henüz Stop-Loss kaynaklı kayıtlı bir hata dersi bulunmuyor. "
                    "Herhangi bir işlem SL olduğunda giriş/çıkış koşulları analiz edilip buraya kaydedilir."
                )
                return
            lines = [f"📚 *Öğrenilen Kurallar ({len(lessons)} Kayıt)*\n"]
            for idx, item in enumerate(lessons, start=1):
                sym = _clean_md(item.get("symbol", "?"))
                reg = _clean_md(str(item.get("regime", "")).upper())
                rsi = float(item.get("entry_rsi", 50.0))
                ob = float(item.get("orderbook_imbalance", 0.0))
                pnl = float(item.get("pnl", 0.0))
                pen = float(item.get("confidence_penalty", 0.05)) * 100.0
                reason = _clean_md(item.get("failure_reason", ""))[:140]
                lines.append(
                    f"{idx}. *{sym}* (`{reg}`) | K/Z: `${pnl:+,.2f}` | RSI: `{rsi:.1f}` | OB: `{ob:+.2f}` | Ceza: `-%{pen:.0f}`\n"
                    f"   ↳ _{reason}_"
                )
            await self.send_message("\n".join(lines))

        elif cmd in ("/audit", "/brier"):
            if not self.db:
                await self.send_message("❌ Veritabanı bağlantısı henüz hazır değil.")
                return
            latest = self.db.get_latest_brier_calibration()
            if not latest:
                await self.send_message("ℹ️ Henüz gece denetim raporu bulunmuyor.")
                return
            msg = (
                f"🌙 *Son 24s Brier Kalibrasyon Özeti*\n\n"
                f"• *Tarih:* `{_clean_md(latest.get('review_date'))}`\n"
                f"• *Brier Skoru:* `{latest.get('brier_score', 0):.4f}`\n"
                f"• *İşlem Sayısı:* `{latest.get('sample_count', 0)}`\n"
                f"• *Min Güven Eşiği:* `%{latest.get('min_confidence_threshold', 0.8)*100:.0f}`\n"
                f"• *Maks Pozisyon:* `${latest.get('max_position_usd', 25.0):,.2f}`"
            )
            await self.send_message(msg)

        elif cmd in ("/panic", "/stop"):
            if not self.risk_engine:
                if self.db:
                    self.db.set_system_halted(True, reason="Telegram /panic komutu")
                await self.send_message("🚨 *ACİL DURUM:* Sistem Telegram üzerinden durduruldu!")
                return
            res = await self.risk_engine.trigger_emergency_kill_switch(
                reason="Telegram /panic komutu verildi"
            )
            await self.send_message(
                f"🚨 *ACİL DURUM BUTONU DEVREDE!*\n\n"
                f"• İptal Edilen Emir: `{res.get('cancelled_orders', 0)}`\n"
                f"• Kapatılan Pozisyon: `{res.get('flattened_positions', 0)}`\n"
                f"• Sistem Durumu: `HALTED (KİLİTLİ)`"
            )

        elif cmd in ("/baslat", "/başlat", "/resume"):
            if self.db:
                self.db.set_system_halted(False, reason="Telegram /resume komutu")
                await self.send_message("✅ *Sistem tekrar aktif edildi.* Otonom tarama ve reflex döngüsü devam ediyor.")
            else:
                await self.send_message("❌ Veritabanına erişilemedi.")

        else:
            await self.send_message(
                f"❓ Bilinmeyen komut: `{_clean_md(cmd)}`\n"
                f"Kullanılabilir komutlar: `/durum`, `/liste`, `/pozisyonlar`, `/audit`, `/panic`, `/baslat`"
            )
