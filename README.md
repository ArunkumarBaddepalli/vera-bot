# Vera Bot — magicpin AI Challenge

**Participant**: Arun Kumar Baddepalli  
**Email**: baddepalliarunkumar@gmail.com  
**Model**: claude-sonnet-4-6 (Anthropic)

---

## Approach

4-context LLM composer using Claude with trigger-kind dispatch:

1. **Context storage** — all 4 types (category, merchant, customer, trigger) stored in-memory, versioned. New version atomically replaces old; next composition uses latest data.

2. **Composition** — structured prompt includes:
   - Category voice rules, taboo words, peer benchmarks, full digest
   - Merchant's exact performance numbers + CTR vs peer gap
   - Trigger payload with resolved digest item IDs → full content (source, trial_n, %)
   - Per-trigger-kind instructions (research_digest → cite source; perf_dip → quote exact delta; recall_due → real slots + price)

3. **Trigger dispatch** — different compose instructions per `kind` baked into prompt so Claude picks the right framing automatically.

4. **Multi-turn handling**:
   - Auto-reply detection via regex (3 patterns → end, 2 → wait 24h, 1 → flag for owner)
   - Opt-out detection → immediate `end`
   - Intent-yes detection on turn ≥ 2 → switch to action mode immediately (no more qualifying questions)
   - Off-topic → one-line polite decline + redirect via LLM

5. **Hard rule enforcement** post-LLM: strip any URLs that slip through (Meta policy), ensure required fields present.

---

## Tradeoffs

| Decision | Tradeoff |
|----------|----------|
| In-memory state | Simple; sufficient for 60-min test window. Restart = state loss. |
| temperature=0 | Deterministic output for same inputs. Slightly less creative but reproducible. |
| Single prompt per composition | No RAG or fine-tuning. Relies on Claude instruction-following. Full context in one shot. |
| claude-sonnet-4-6 | Good quality, fast enough for 30s budget. Opus-4-7 would score higher but risks timeout on complex prompts. |

---

## Additional context that would have helped

1. **Merchant's real appointment calendar** — needed for `recall_due` and `appointment_tomorrow` to offer genuine open slots instead of payload-provided ones.
2. **Live Google Trends data per locality** — trend_signals in category context are static; real-time data would enable more timely `competitor_opened` and `festival` framing.
3. **Merchant's prior sent-message history** — to enforce anti-repetition across sessions, not just within a conversation.
4. **Customer phone number** — for `chronic_refill_due` and `recall_due` to reference delivery address or booking confirmation.

---

## Local Testing

```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=<your-key> uvicorn bot:app --host 0.0.0.0 --port 8080
```

Run the judge simulator (edit `BOT_URL` and `LLM_API_KEY` at top of file):
```bash
python judge_simulator.py
```
