// External watchdog for the VPS AI stack, run by Cloudflare cron every minute.
// Lives outside the host, so host/network/provider death stops nothing here.
// Alerts go to Telegram via the notifications bot; state lives in KV so a
// prolonged outage is re-announced every 6h instead of every minute.

const TARGETS = [
  { url: "https://llm.esmien.ru/webui-health", ok: [200], name: "origin/webui" },
  { url: "https://ai.esmien.ru/", ok: [200, 403], name: "cloudflare/webui" },
];

const RE_ALERT_MS = 6 * 3600 * 1000;
const TG_CHAT = "-1004410657647";
const TG_THREAD = "2";

async function probe(target) {
  let code = "000";
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetch(target.url, {
        redirect: "manual",
        cf: { cacheTtl: 0, cacheEverything: false },
        signal: AbortSignal.timeout(12000),
      });
      code = String(res.status);
      if (target.ok.includes(res.status)) return { ok: true, code };
    } catch (e) {
      code = "net:" + (e && e.name ? e.name : "error");
    }
    if (attempt === 0) await new Promise((r) => setTimeout(r, 4000));
  }
  return { ok: false, code };
}

async function sendTelegram(env, text) {
  if (!env.TG_BOT_TOKEN) return "no token";
  const body = new URLSearchParams({
    chat_id: TG_CHAT,
    message_thread_id: TG_THREAD,
    disable_web_page_preview: "true",
    text,
  });
  try {
    const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      body,
    });
    return res.ok ? "sent" : `tg ${res.status}`;
  } catch (e) {
    return "tg net error";
  }
}

async function check(env) {
  const results = await Promise.all(TARGETS.map(async (t) => ({ ...t, ...(await probe(t)) })));
  const bad = results.filter((r) => !r.ok);
  const now = Date.now();
  const raw = await env.STATE.get("state");
  const prev = raw ? JSON.parse(raw) : { status: "ok", last_alert: 0 };
  let action = "none";

  if (bad.length > 0) {
    const detail = bad.map((r) => `${r.name} -> ${r.code}`).join("\n");
    if (prev.status !== "fail" || now - prev.last_alert >= RE_ALERT_MS) {
      const minutes = prev.status === "fail" ? Math.round((now - prev.last_alert) / 60000) : 0;
      const lead = prev.status === "fail" ? `🔴 VPS всё ещё не отвечает (${minutes} мин)` : "🔴 VPS не отвечает";
      await sendTelegram(
        env,
        `${lead}\n${detail}\nВремя: ${new Date(now).toISOString().replace(/\.\d+Z$/, "Z")}\n` +
          `Проверка: Cloudflare Worker, каждую минуту`,
      );
      action = "alert";
      await env.STATE.put("state", JSON.stringify({ status: "fail", last_alert: now }));
    } else {
      await env.STATE.put("state", JSON.stringify({ status: "fail", last_alert: prev.last_alert || 0 }));
    }
  } else if (prev.status === "fail") {
    const downtime = Math.round((now - (prev.last_alert || now)) / 60000);
    await sendTelegram(env, `🟢 VPS снова отвечает (простой ~${downtime} мин)\nВремя: ${new Date(now).toISOString().replace(/\.\d+Z$/, "Z")}`);
    action = "recovery";
    await env.STATE.put("state", JSON.stringify({ status: "ok", last_alert: 0 }));
  } else {
    await env.STATE.put("state", JSON.stringify({ status: "ok", last_alert: 0 }));
  }

  return { action, results: results.map((r) => ({ name: r.name, ok: r.ok, code: r.code })) };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(check(env));
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.searchParams.has("drill")) {
      const out = await sendTelegram(env, `[ТЕСТ] внешний сторож Cloudflare: канал алертов жив`);
      return new Response(`drill: ${out}\n`, { headers: { "content-type": "text/plain" } });
    }
    const out = await check(env);
    return new Response(JSON.stringify(out, null, 1) + "\n", {
      headers: { "content-type": "application/json" },
    });
  },
};
