"""Web dashboard for real-time monitoring."""

from __future__ import annotations

import asyncio

from aiohttp import web

import structlog

from trading_agent.monitoring.metrics import MetricsCollector

logger = structlog.get_logger(__name__)


class DashboardServer:
    def __init__(self, metrics: MetricsCollector, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._metrics = metrics
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/api/metrics", self._api_metrics)
        self._app.router.add_get("/api/pnl", self._api_pnl)
        self._app.router.add_get("/api/trades", self._api_trades)
        self._app.router.add_get("/api/system", self._api_system)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("dashboard_started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=_DASHBOARD_HTML, content_type="text/html")

    async def _api_metrics(self, request: web.Request) -> web.Response:
        return web.json_response(self._metrics.export_metrics())

    async def _api_pnl(self, request: web.Request) -> web.Response:
        return web.json_response(self._metrics.get_pnl_summary())

    async def _api_trades(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        trades = self._metrics.get_recent_trades(limit)
        return web.json_response([{
            "timestamp": t.timestamp.isoformat(), "strategy": t.strategy, "symbol": t.symbol,
            "side": t.side, "price": t.price, "size": t.size,
            "value_usdt": t.value_usdt, "pnl_usdt": t.pnl_usdt,
        } for t in trades])

    async def _api_system(self, request: web.Request) -> web.Response:
        sm = self._metrics.get_system_metrics()
        return web.json_response({
            "uptime_seconds": sm.uptime_seconds, "websocket_connected": sm.websocket_connected,
            "events_processed": sm.events_processed,
            "last_heartbeat": sm.last_heartbeat.isoformat() if sm.last_heartbeat else None,
        })


_DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><title>Crypto Trading Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#c9d1d9}
.container{max-width:1400px;margin:0 auto;padding:20px}
h1{color:#58a6ff;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:20px}
.card h2{color:#58a6ff;font-size:14px;margin-bottom:15px;text-transform:uppercase;letter-spacing:1px}
.metric{display:flex;justify-content:space-between;margin-bottom:10px}
.metric-label{color:#8b949e}
.metric-value{font-weight:600}
.positive{color:#3fb950}.negative{color:#f85149}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px;border-bottom:1px solid #30363d}
th{color:#8b949e;font-weight:500;font-size:12px;text-transform:uppercase}
.loading{text-align:center;padding:40px;color:#8b949e}
</style></head><body>
<div class="container"><h1>Crypto Trading Agent</h1>
<div class="grid">
<div class="card"><h2>System Status</h2><div id="system-status" class="loading">Loading...</div></div>
<div class="card"><h2>P&L Summary</h2><div id="pnl-summary" class="loading">Loading...</div></div>
</div>
<div class="card"><h2>Strategy Performance</h2>
<table><thead><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>Total P&L</th><th>Volume</th></tr></thead>
<tbody id="strategy-table"></tbody></table></div>
<div class="card" style="margin-top:20px"><h2>Recent Trades</h2>
<table><thead><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Price</th><th>Size</th><th>P&L</th></tr></thead>
<tbody id="trades-table"></tbody></table></div></div>
<script>
async function fetchMetrics(){const[m,p,t,s]=await Promise.all([fetch('/api/metrics').then(r=>r.json()),fetch('/api/pnl').then(r=>r.json()),fetch('/api/trades?limit=20').then(r=>r.json()),fetch('/api/system').then(r=>r.json())]);
document.getElementById('system-status').innerHTML=`<div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">${Math.floor(s.uptime_seconds/60)}m</span></div><div class="metric"><span class="metric-label">WS</span><span class="metric-value ${s.websocket_connected?'positive':'negative'}">${s.websocket_connected?'Connected':'Disconnected'}</span></div>`;
const pc=p.total_pnl_usdt>=0?'positive':'negative';
document.getElementById('pnl-summary').innerHTML=`<div class="metric"><span class="metric-label">Total P&L</span><span class="metric-value ${pc}">$${p.total_pnl_usdt.toFixed(2)}</span></div><div class="metric"><span class="metric-label">Trades</span><span class="metric-value">${p.total_trades}</span></div><div class="metric"><span class="metric-label">Volume</span><span class="metric-value">$${p.total_volume_usdt.toFixed(0)}</span></div>`;
document.getElementById('strategy-table').innerHTML=Object.entries(m.strategies).map(([n,s])=>{const c=s.total_pnl_usdt>=0?'positive':'negative';return`<tr><td>${n}</td><td>${s.total_trades}</td><td>${(s.win_rate*100).toFixed(1)}%</td><td class="${c}">$${s.total_pnl_usdt.toFixed(2)}</td><td>$${s.total_volume_usdt.toFixed(0)}</td></tr>`}).join('')||'<tr><td colspan="5">No trades</td></tr>';
document.getElementById('trades-table').innerHTML=t.map(t=>`<tr><td>${new Date(t.timestamp).toLocaleTimeString()}</td><td>${t.strategy}</td><td>${t.symbol}</td><td class="${t.side==='BUY'?'positive':'negative'}">${t.side}</td><td>$${t.price.toFixed(4)}</td><td>${t.size.toFixed(4)}</td><td class="${(t.pnl_usdt||0)>=0?'positive':'negative'}">${t.pnl_usdt!==null?'$'+t.pnl_usdt.toFixed(2):'-'}</td></tr>`).join('')||'<tr><td colspan="7">No trades</td></tr>'}
fetchMetrics();setInterval(fetchMetrics,5000);
</script></body></html>"""
