/**
 * DealScope Feed — public, read-only deal feed for the scanner.
 * Revision 3: D1 binding is named DEALS, so it arrives as env.DEALS.
 *
 * Endpoints:
 *   GET  /            static page (rendered client-side from /api/deals)
 *   GET  /api/deals   public deal list (?category=&marketplace=&limit=)
 *   GET  /api/health  uptime check, reports deal count and last push
 *   POST /api/push    scanner -> feed upload (Bearer PUSH_TOKEN, <= 100 deals)
 *
 * The scanner owns all discovery, matching and valuation. This worker only
 * validates, stores and serves what was pushed; writes are guarded by the
 * secret token so the public site stays read-only for everyone else.
 */

const MAX_BATCH = 100;
const MAX_TITLE = 200;
const MAX_URL = 600;
const RETENTION_DAYS = 7;

const RESPONSE_HEADERS = {
    "content-type": "application/json",
    "cache-control": "no-store",
};

function json(data, status = 200) {
    return new Response(JSON.stringify(data), { status, headers: RESPONSE_HEADERS });
}

function nowIso() {
    return new Date().toISOString();
}

function cleanText(value, maxLength, fallback = "") {
    if (typeof value !== "string") return fallback;
    return value.trim().slice(0, maxLength);
}

function cleanInt(value, fallback = null) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(0, Math.round(parsed));
}

function asBooleanOrNull(value) {
    if (value === true) return true;
    if (value === false) return false;
    return null;
}

function validDeal(raw) {
    const deal = typeof raw === "object" && raw !== null ? raw : {};
    const item = {};
    item.item_id = cleanText(deal.item_id, 64);
    if (!item.item_id) return null;
    item.marketplace = cleanText(deal.marketplace, 12);
    if (!/^EBAY_[A-Z]{2,3}$/.test(item.marketplace)) return null;
    item.product_id = cleanText(deal.product_id, 60);
    item.product_label = cleanText(deal.product_label, 80) || item.product_id;
    item.title = cleanText(deal.title, MAX_TITLE);
    if (!item.title) return null;
    item.url = cleanText(deal.url, MAX_URL);
    if (!/^https:\/\//.test(item.url)) return null;
    item.thumbnail_url = cleanText(deal.thumbnail_url, MAX_URL);
    if (item.thumbnail_url && !/^https:\/\//.test(item.thumbnail_url)) item.thumbnail_url = "";
    item.condition_label = cleanText(deal.condition_label, 60) || "unknown";
    item.category = cleanText(deal.category, 30);
    item.price_eur = cleanInt(deal.price_eur);
    item.total_eur = cleanInt(deal.total_eur, item.price_eur ?? 0) ?? 0;
    item.fair_value_eur = cleanInt(deal.fair_value_eur);
    item.discount_pct = cleanInt(deal.discount_pct);
    item.deal_score = cleanInt(deal.deal_score) ?? 0;
    item.confidence = cleanText(deal.confidence, 20) || "low";
    item.ends_at = cleanText(deal.ends_at, 40);
    if (item.ends_at && !/^\d{4}-/.test(item.ends_at)) item.ends_at = "";
    item.observed_at = cleanText(deal.observed_at, 40) || nowIso();
    if (!/^\d{4}-/.test(item.observed_at)) item.observed_at = nowIso();
    item.search_ok = asBooleanOrNull(deal.search_ok);
    item.shipping_ok = asBooleanOrNull(deal.shipping_ok);
    item.import_estimated = asBooleanOrNull(deal.import_estimated);
    return item;
}

async function ensureSchema(db) {
    await db.batch([
        db.prepare(
            `CREATE TABLE IF NOT EXISTS deals (
                item_id TEXT PRIMARY KEY,
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_label TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                thumbnail_url TEXT,
                condition_label TEXT,
                category TEXT,
                price_eur INTEGER NOT NULL,
                total_eur INTEGER NOT NULL,
                fair_value_eur INTEGER,
                discount_pct INTEGER,
                deal_score INTEGER NOT NULL DEFAULT 0,
                confidence TEXT,
                ends_at TEXT,
                observed_at TEXT,
                search_ok INTEGER,
                shipping_ok INTEGER,
                import_estimated INTEGER,
                pushed_at TEXT NOT NULL
            )`,
        ),
        db.prepare(
            `CREATE TABLE IF NOT EXISTS feed_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )`,
        ),
        db.prepare("CREATE INDEX IF NOT EXISTS idx_deals_score ON deals (deal_score DESC)"),
        db.prepare("CREATE INDEX IF NOT EXISTS idx_deals_category ON deals (category)"),
    ]);
}

function database(env) {
    return env.DEALS;
}

async function handlePush(request, env) {
    const authorization = request.headers.get("authorization") || "";
    const expected = "Bearer " + (env.PUSH_TOKEN || "");
    if (!env.PUSH_TOKEN || authorization !== expected) {
        return json({ error: "unauthorized" }, 401);
    }

    let payload;
    try {
        payload = await request.json();
    } catch {
        return json({ error: "invalid json body" }, 400);
    }
    const rows = Array.isArray(payload && payload.deals) ? payload.deals : null;
    if (!rows) {
        return json({ error: "expected body: {\"deals\": [...]}" }, 400);
    }
    if (rows.length > MAX_BATCH) {
        return json({ error: "batch too large (max 100)" }, 413);
    }

    const deals = [];
    const duplicatedIds = new Set();
    for (const raw of rows) {
        const deal = validDeal(raw);
        if (!deal) return json({ error: "invalid deal payload", index: deals.length }, 422);
        if (duplicatedIds.has(deal.item_id)) {
            return json({ error: "duplicate item_id in batch", item_id: deal.item_id }, 422);
        }
        duplicatedIds.add(deal.item_id);
        deals.push(deal);
    }

    const db = database(env);
    await ensureSchema(db);
    const pushedAt = nowIso();
    const statements = deals.map((deal) =>
        db.prepare(
            `INSERT INTO deals (
                item_id, marketplace, product_id, product_label, title, url,
                thumbnail_url, condition_label, category, price_eur, total_eur,
                fair_value_eur, discount_pct, deal_score, confidence, ends_at,
                observed_at, search_ok, shipping_ok, import_estimated, pushed_at
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21)
            ON CONFLICT(item_id) DO UPDATE SET
                marketplace = excluded.marketplace,
                product_id = excluded.product_id,
                product_label = excluded.product_label,
                title = excluded.title,
                url = excluded.url,
                thumbnail_url = excluded.thumbnail_url,
                condition_label = excluded.condition_label,
                category = excluded.category,
                price_eur = excluded.price_eur,
                total_eur = excluded.total_eur,
                fair_value_eur = excluded.fair_value_eur,
                discount_pct = excluded.discount_pct,
                deal_score = excluded.deal_score,
                confidence = excluded.confidence,
                ends_at = excluded.ends_at,
                observed_at = excluded.observed_at,
                search_ok = excluded.search_ok,
                shipping_ok = excluded.shipping_ok,
                import_estimated = excluded.import_estimated,
                pushed_at = excluded.pushed_at`,
        )
            .bind(
                deal.item_id,
                deal.marketplace,
                deal.product_id,
                deal.product_label,
                deal.title,
                deal.url,
                deal.thumbnail_url,
                deal.condition_label,
                deal.category,
                deal.price_eur,
                deal.total_eur,
                deal.fair_value_eur,
                deal.discount_pct,
                deal.deal_score,
                deal.confidence,
                deal.ends_at,
                deal.observed_at,
                deal.search_ok,
                deal.shipping_ok,
                deal.import_estimated,
                pushedAt,
            ),
    );
    statements.push(
        db.prepare(
            "INSERT INTO feed_meta (key, value) VALUES ('last_push', ?1) " +
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ).bind(pushedAt),
    );
    statements.push(
        db.prepare(
            `DELETE FROM deals WHERE observed_at < ?1`,
        ).bind(new Date(Date.now() - RETENTION_DAYS * 86400000).toISOString()),
    );
    await db.batch(statements);

    return json({ ok: true, pushed: deals.length, pushed_at: pushedAt });
}

async function handleList(request, env) {
    const db = database(env);
    await ensureSchema(db);
    const url = new URL(request.url);
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit")) || 50));
    const category = cleanText(url.searchParams.get("category"), 30);
    const marketplace = cleanText(url.searchParams.get("marketplace"), 12);

    let statement;
    if (category && marketplace) {
        statement = db.prepare(
            "SELECT * FROM deals WHERE category = ?1 AND marketplace = ?2 ORDER BY deal_score DESC LIMIT ?3",
        ).bind(category, marketplace, limit);
    } else if (category) {
        statement = db.prepare(
            "SELECT * FROM deals WHERE category = ?1 ORDER BY deal_score DESC LIMIT ?2",
        ).bind(category, limit);
    } else if (marketplace) {
        statement = db.prepare(
            "SELECT * FROM deals WHERE marketplace = ?1 ORDER BY deal_score DESC LIMIT ?2",
        ).bind(marketplace, limit);
    } else {
        statement = db.prepare("SELECT * FROM deals ORDER BY deal_score DESC LIMIT ?1").bind(limit);
    }

    const { results } = await statement.all();
    const meta = await db.prepare("SELECT value FROM feed_meta WHERE key = 'last_push'").first();
    return json({
        updated_at: meta ? meta.value : null,
        count: results.length,
        deals: results,
    });
}

async function handleHealth(env) {
    const db = database(env);
    if (!db) {
        return json({ ok: false, error: "D1 binding (DEALS) is not attached" }, 503);
    }
    await ensureSchema(db);
    const row = await db.prepare("SELECT COUNT(*) AS n FROM deals").first();
    const meta = await db.prepare("SELECT value FROM feed_meta WHERE key = 'last_push'").first();
    return json({
        ok: true,
        deals: row ? row.n : 0,
        last_push: meta ? meta.value : null,
    });
}

export default {
    async fetch(request, env) {
        try {
            const url = new URL(request.url);
            if (url.pathname === "/api/health") {
                return await handleHealth(env);
            }
            if (url.pathname === "/api/deals" && request.method === "GET") {
                return await handleList(request, env);
            }
            if (url.pathname === "/api/push" && request.method === "POST") {
                return await handlePush(request, env);
            }
            if (url.pathname.startsWith("/api/")) {
                return json({ error: "not found" }, 404);
            }
            return env.ASSETS.fetch(request);
        } catch (error) {
            return json({ error: "internal error", detail: String(error && error.message || error) }, 500);
        }
    },
};
