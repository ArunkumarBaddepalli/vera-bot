#!/usr/bin/env python3
"""
Vera Bot — magicpin AI Challenge
Participant: Arun Kumar Baddepalli
"""

import os, time, json, re, uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import anthropic

# ─── Config ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Vera Bot")
BOOT_TIME = time.time()
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ─── In-memory state ─────────────────────────────────────────────────────────
# (scope, context_id) → {version: int, payload: dict}
contexts: dict[tuple[str, str], dict] = {}
# conversation_id → list of turns
conversations: dict[str, list] = {}
# suppression keys already fired
fired_suppression: set[str] = set()

# ─── Pattern detection ────────────────────────────────────────────────────────

_AUTO_PATTERNS = [
    r"thank you for contacting",
    r"our team will (respond|get back|reply)",
    r"this is an? (automated|auto) (message|reply|response)",
    r"we will get back to you",
    r"currently unavailable",
    r"aapki jaankari ke liye.*shukriya",
    r"main.*automated assistant",
    r"hi,?\s+thanks for (reaching|contacting)",
    r"we have received your (message|query|inquiry)",
    r"will respond (shortly|soon|as soon as)",
    r"auto.?reply",
]

_OPT_OUT = [
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"not interested",
    r"don'?t (message|contact|text|send)",
    r"remove me",
    r"leave me alone",
    r"please stop",
    r"stop (messaging|sending|contacting)",
    r"band karo",
    r"mat bhejo",
    r"nahi chahiye",
    r"useless.*spam|spam.*useless",
    r"go away",
]

_INTENT_YES = [
    r"\b(yes|yeah|yep|sure)\b",
    r"\b(ok|okay)\b",
    r"\bgo ahead\b",
    r"\bproceed\b",
    r"\bdo it\b",
    r"let'?s do it",
    r"sounds good",
    r"\bplease (send|draft|do|proceed|go)\b",
    r"\b(haan|bilkul|theek hai|kar do|bhejo|chalega)\b",
    r"what.?s next",
    r"\bconfirm\b",
    r"i want (to|this)",
    r"great.*next|next.*great",
]


def _match(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def is_auto_reply(msg: str) -> bool:
    return _match(_AUTO_PATTERNS, msg)


def is_opt_out(msg: str) -> bool:
    return _match(_OPT_OUT, msg)


def is_intent_yes(msg: str) -> bool:
    return _match(_INTENT_YES, msg)


def consecutive_auto_replies(turns: list[dict]) -> int:
    merchant_msgs = [t["message"] for t in turns if t.get("from_role") == "merchant"]
    count = 0
    for msg in reversed(merchant_msgs):
        if is_auto_reply(msg):
            count += 1
        else:
            break
    return count


# ─── Context helpers ──────────────────────────────────────────────────────────

def get_payload(scope: str, cid: str) -> Optional[dict]:
    entry = contexts.get((scope, cid))
    return entry["payload"] if entry else None


def resolve_item(category: dict, item_id: str) -> Optional[dict]:
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    for item in category.get("patient_content_library", []):
        if item.get("id") == item_id:
            return item
    return None


def count_contexts() -> dict:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return counts


# ─── Composition prompts ──────────────────────────────────────────────────────

_COMPOSE_SYSTEM = """You are Vera, magicpin's WhatsApp AI assistant for merchants.
You write one outbound WhatsApp message per call.

=== HARD RULES (violating any = score penalty) ===
1. Zero fabrication — use ONLY data from the provided contexts. No invented stats, no fake research citations, no fake competitor names.
2. No URLs — Meta template policy. Hard fail if any URL in body.
3. One CTA only — never two asks in one message. Binary YES/STOP beats open-ended for action triggers.
4. No re-introduction ("Hi I'm Vera") after first contact.
5. No spam/promo tone. Peer/colleague register only. Never "AMAZING DEAL!" or "HURRY NOW!".
6. Language match — if merchant speaks hi-en mix, blend Hindi + English naturally.
7. Clinical categories (dentists, pharmacies): use technical vocabulary, strictly avoid taboo words.
8. Specific beats generic — "Dental Cleaning @ ₹299" beats "Flat 20% off".
9. Keep body concise — WhatsApp, not email. Under 180 words.
10. CTA lands in the LAST sentence.

=== WHAT SCORES 9-10/10 ===
- Specificity: cite actual numbers from context (trial_n, delta_pct, batch numbers, peer stats)
- Source citation: "JIDA Oct 2026 p.14" / "DCI circular 2026-11-04" — always at end of claim
- Trigger hook: make WHY NOW explicit in first or second sentence
- Merchant personalization: use THEIR performance numbers, THEIR locality, THEIR active offer title
- Compulsion lever (pick ONE): curiosity ("want to see who?"), loss aversion ("you're missing X"), reciprocity ("I noticed Y"), effort externalization ("I've drafted X — just say go")

=== ANTI-PATTERNS (each one loses points) ===
- "I hope you're doing well" preambles
- Multiple CTAs in one message
- Generic "increase your sales" / "boost your revenue" language
- Restating what Vera already told the merchant in previous turn
- Promotional tone for clinical categories

=== OUTPUT FORMAT ===
Reply with raw JSON only. No markdown code blocks. No explanation before or after.
{"body": "...", "cta": "open_ended|binary_yes_no|binary_confirm_cancel|multi_choice_slot|none", "send_as": "vera|merchant_on_behalf", "suppression_key": "...", "rationale": "..."}"""


def _build_compose_prompt(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
) -> str:
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    voice = category.get("voice", {})
    active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    signals = merchant.get("signals", [])
    cust_agg = merchant.get("customer_aggregate", {})
    rev_themes = merchant.get("review_themes", [])
    conv_hist = merchant.get("conversation_history", [])
    trig_payload = trigger.get("payload", {})

    # Resolve digest items referenced by ID in trigger payload
    resolved = []
    for key in ("top_item_id", "alert_id", "digest_item_id"):
        item_id = trig_payload.get(key)
        if item_id:
            item = resolve_item(category, item_id)
            if item:
                resolved.append(item)

    # CTR comparison
    merchant_ctr = perf.get("ctr", 0)
    peer_ctr = peer.get("avg_ctr", 0)
    ctr_label = "above peer" if merchant_ctr >= peer_ctr else "BELOW peer"
    ctr_gap = round(abs(merchant_ctr - peer_ctr), 3)

    delta = perf.get("delta_7d", {})

    # Customer block
    if customer:
        c_id = customer.get("identity", {})
        c_rel = customer.get("relationship", {})
        c_pref = customer.get("preferences", {})
        customer_block = f"""=== CUSTOMER (send on merchant's behalf) ===
Name: {c_id.get("name")}
Language preference: {c_id.get("language_pref")}
State: {customer.get("state")}
Last visit: {c_rel.get("last_visit")}
Total visits: {c_rel.get("visits_total")}
Services received: {c_rel.get("services_received", [])}
Lifetime value: ₹{c_rel.get("lifetime_value", 0)}
Preferred slots: {c_pref.get("preferred_slots")}
Consent scope: {customer.get("consent", {}).get("scope", [])}
→ send_as MUST be "merchant_on_behalf"
→ Address customer by name, match their language_pref"""
    else:
        customer_block = "Customer: none → send_as MUST be \"vera\""

    # Last conversation touch
    last_touch = "no prior conversation"
    if conv_hist:
        last = conv_hist[-1]
        last_touch = (
            f"{last.get('from','?')} said: \"{last.get('body','')[:150]}\""
            f" [engagement: {last.get('engagement','?')}]"
        )

    # Top digest items from category
    top_digest = json.dumps(category.get("digest", [])[:6], ensure_ascii=False)

    return f"""=== CATEGORY: {category.get("slug")} ===
Voice: tone={voice.get("tone")}, code_mix={voice.get("code_mix", False)}
Taboo words (NEVER use): {voice.get("vocab_taboo", [])}
Peer benchmarks: avg_rating={peer.get("avg_rating")}, avg_ctr={peer.get("avg_ctr")}, avg_reviews={peer.get("avg_reviews")}
Sample catalog offers: {[o.get("title") for o in category.get("offer_catalog", [])[:5]]}
Category digest (latest items): {top_digest}

=== MERCHANT ===
Business name: {identity.get("name")}
Owner first name: {identity.get("owner_first_name")}
Location: {identity.get("locality")}, {identity.get("city")}
Languages: {identity.get("languages", ["en"])}
GBP verified: {identity.get("verified")}
Subscription: status={merchant.get("subscription", {}).get("status")}, plan={merchant.get("subscription", {}).get("plan")}, days_remaining={merchant.get("subscription", {}).get("days_remaining")}
Performance (30d): views={perf.get("views")}, calls={perf.get("calls")}, directions={perf.get("directions")}, CTR={merchant_ctr} ({ctr_label} peer median {peer_ctr}, gap={ctr_gap})
7-day delta: views={delta.get("views_pct", 0):+.0%}, calls={delta.get("calls_pct", 0):+.0%}
Active offers: {[o.get("title") for o in active_offers] or ["none"]}
Signals: {signals}
Customer aggregate: {json.dumps(cust_agg)}
Review themes: {[(t.get("theme"), t.get("sentiment"), t.get("occurrences_30d")) for t in rev_themes]}
Last Vera touch: {last_touch}

=== TRIGGER ===
Kind: {trigger.get("kind")}
Source: {trigger.get("source")} | Scope: {trigger.get("scope")} | Urgency: {trigger.get("urgency")}/5
Full payload: {json.dumps(trig_payload, ensure_ascii=False)}
Resolved digest items (CITE THESE): {json.dumps(resolved, ensure_ascii=False) if resolved else "none — derive from category digest above"}
Suppression key: {trigger.get("suppression_key")}

{customer_block}

=== COMPOSE INSTRUCTIONS ===
Trigger kind "{trigger.get("kind")}" → use the following approach:
- research_digest / regulation_change / cde_opportunity: cite source + key stat, offer to act on it
- perf_dip / perf_spike / seasonal_perf_dip: quote exact merchant numbers, compare to peer, give one concrete action
- recall_due / chronic_refill_due: address customer by name, use real slots/dates from payload
- competitor_opened: use curiosity/loss-aversion, don't name-drop unless competitor in payload
- festival_upcoming / ipl_match_today: tie merchant's category to the event specifically
- active_planning_intent: respond to last merchant message, draft the artifact immediately
- renewal_due / winback_eligible: use loss framing (what they're missing), single binary CTA
- dormant_with_vera / curious_ask_due: low-pressure, curiosity-driven, no commitment ask
- milestone_reached: celebrate + attach next action
- review_theme_emerged: quote the theme + concrete response strategy
- supply_alert: urgency=5, cite batch numbers, offer to handle affected customers
- default: specificity + one compulsion lever + clear CTA

Write the message. Reply with raw JSON only."""


def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> dict:
    prompt = _build_compose_prompt(category, merchant, trigger, customer)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            temperature=0,
            system=_COMPOSE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fences if present despite instructions
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())

        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            result = json.loads(m.group())
            for f in ["body", "cta", "send_as", "suppression_key", "rationale"]:
                if f not in result:
                    result[f] = "" if f not in ("send_as", "cta") else (
                        "vera" if f == "send_as" else "none"
                    )
            # Hard rule: strip any URLs that slipped through
            result["body"] = re.sub(r"https?://\S+", "", result["body"]).strip()
            return result
    except Exception as e:
        print(f"[compose error] {e}")

    # Fallback — minimal safe message
    owner = merchant.get("identity", {}).get("owner_first_name", "")
    biz = merchant.get("identity", {}).get("name", "")
    return {
        "body": f"Hi {owner or biz}, quick update on your account — reply YES for details.",
        "cta": "binary_yes_no",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", f"fallback:{uuid.uuid4()}"),
        "rationale": "Fallback due to composition error.",
    }


# ─── Reply handler ─────────────────────────────────────────────────────────────

_REPLY_SYSTEM = """You are Vera, magicpin's WhatsApp AI assistant.
Respond to a merchant's latest message in an ongoing conversation.

Rules:
1. If merchant confirmed/agreed → switch to action mode immediately. Draft the artifact, no more qualifying questions.
2. If off-topic question → one-line polite decline, redirect to original topic.
3. If asking for clarification → answer concisely with facts from context only.
4. No URLs. No re-introduction. Under 100 words.
5. CTA in last sentence.

OUTPUT: raw JSON only.
{"action": "send", "body": "...", "cta": "open_ended|binary_yes_no|none", "rationale": "..."}"""


def compose_reply(
    category: dict,
    merchant: dict,
    turns: list[dict],
    latest_message: str,
    customer: Optional[dict],
) -> dict:
    identity = merchant.get("identity", {})
    active_offers = [
        o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active"
    ]
    convo = "\n".join(
        f"[{t.get('from_role','?').upper()}] {t.get('message','')}"
        for t in turns[-8:]
    )
    prompt = f"""Merchant: {identity.get("name")}, {identity.get("city")}
Languages: {identity.get("languages", ["en"])}
Active offers: {active_offers}
Category: {category.get("slug")}

Conversation:
{convo}

Merchant's latest message: "{latest_message}"

Respond. If they confirmed → take action. If off-topic → decline + redirect.
Raw JSON only: {{"action": "send", "body": "...", "cta": "open_ended|binary_yes_no|none", "rationale": "..."}}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=512,
            temperature=0,
            system=_REPLY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            result = json.loads(m.group())
            result["body"] = re.sub(r"https?://\S+", "", result.get("body", "")).strip()
            return result
    except Exception as e:
        print(f"[reply error] {e}")

    return {
        "action": "send",
        "body": "Got it! Working on that now — I'll send you the draft shortly.",
        "cta": "none",
        "rationale": "Fallback reply.",
    }


# ─── Pydantic models ──────────────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - BOOT_TIME),
        "contexts_loaded": count_contexts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Arun Kumar Baddepalli",
        "team_members": ["Arun Kumar Baddepalli"],
        "model": MODEL,
        "approach": (
            "4-context LLM composer (Claude) with trigger-kind dispatch, "
            "resolved digest item IDs, auto-reply detection (regex), "
            "intent-transition routing, and adaptive context versioning."
        ),
        "contact_email": "baddepalliarunkumar@gmail.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] >= body.version:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current["version"],
            },
        )
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat() + "Z",
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trg_id in body.available_triggers:
        if len(actions) >= 20:
            break

        trg = get_payload("trigger", trg_id)
        if not trg:
            continue

        # Suppression check
        sup_key = trg.get("suppression_key", "")
        if sup_key and sup_key in fired_suppression:
            continue

        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")

        merchant = get_payload("merchant", merchant_id)
        if not merchant:
            continue

        category_slug = merchant.get("category_slug", "")
        category = get_payload("category", category_slug)
        if not category:
            continue

        customer = get_payload("customer", customer_id) if customer_id else None

        result = compose(category, merchant, trg, customer)

        if sup_key:
            fired_suppression.add(sup_key)

        conv_id = f"conv_{merchant_id}_{trg_id}"
        owner = merchant.get("identity", {}).get("owner_first_name", "")

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result.get("send_as", "vera"),
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind', 'generic')}_v1",
            "template_params": [owner, result.get("body", "")[:120], ""],
            "body": result.get("body", ""),
            "cta": result.get("cta", "none"),
            "suppression_key": result.get("suppression_key", sup_key),
            "rationale": result.get("rationale", ""),
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    message = body.message

    turns = conversations.setdefault(conv_id, [])
    turns.append({
        "from_role": body.from_role,
        "message": message,
        "received_at": body.received_at,
        "turn_number": body.turn_number,
    })

    # Explicit opt-out → end immediately
    if is_opt_out(message):
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out. Closing conversation.",
        }

    # Auto-reply cascade
    auto_count = consecutive_auto_replies(turns)
    if auto_count >= 3:
        return {
            "action": "end",
            "rationale": f"Auto-reply detected {auto_count}x consecutively. No real engagement. Closing.",
        }
    if auto_count == 2:
        return {
            "action": "wait",
            "wait_seconds": 86400,
            "rationale": "Auto-reply 2x. Backing off 24h for owner to check messages.",
        }
    if auto_count == 1:
        return {
            "action": "send",
            "body": "Looks like an automated reply 🤖 When the owner is free — just reply 'Yes' to continue.",
            "cta": "binary_yes_no",
            "rationale": "First auto-reply detected. One prompt to flag for owner.",
        }

    # Intent YES on turn ≥ 2 → action mode
    if is_intent_yes(message) and body.turn_number >= 2:
        merchant = get_payload("merchant", body.merchant_id) if body.merchant_id else {}
        active_offers = [
            o.get("title")
            for o in (merchant or {}).get("offers", [])
            if o.get("status") == "active"
        ]
        offer_line = f" Working with: {active_offers[0]}." if active_offers else ""
        return {
            "action": "send",
            "body": f"Great!{offer_line} Drafting now — I'll have it ready in a moment.",
            "cta": "none",
            "rationale": "Merchant confirmed intent. Switched to action mode immediately.",
        }

    # LLM-based reply for nuanced cases
    merchant = get_payload("merchant", body.merchant_id) if body.merchant_id else {}
    category_slug = (merchant or {}).get("category_slug", "")
    category = get_payload("category", category_slug) or {}
    customer = get_payload("customer", body.customer_id) if body.customer_id else None

    return compose_reply(category, merchant or {}, turns, message, customer)


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    fired_suppression.clear()
    return {"status": "wiped"}
