import os
import json
from pydantic import Field
from pydantic import BaseModel
from typing import Any, Dict, List, Literal, Optional
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from decimal import Decimal


from db import (
    MaterialInfo,
    ensure_schema,
    fetch_builder_material_history,
    fetch_alternative_brands,
    fetch_material_by_name_and_brand,
    fetch_pricing_rules_for_quantity,
)

load_dotenv()

from logger_config import get_logger
logger = get_logger("app")

# =============================================================================
# LLM SETUP
# =============================================================================


def get_llm(model_name: str = "openai/gpt-oss-120b") -> ChatGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")
    return ChatGroq(temperature=0.2, model=model_name, groq_api_key=api_key)

class NegotiationState(BaseModel):

    # Current negotiation context
    intent: Optional[Literal["inquiry", "non_inquiry"]] = None
    builder_name : Optional[str] = None 
    input_material_name: Optional[str] = None
    input_brand: Optional[str] = None
    input_quantity: Optional[int] = None
    input_city: Optional[str] = None
    initial_unit: Optional[str] = None
    builder_offered_price: List[float] = Field(default_factory=list)
    
    # Market & DB data
    market_data: Dict[str, Any] = Field(default_factory=dict)
    material_info: Optional[Dict[str, Any]] = None
    builder_info: Optional[Dict[str, Any]] = None
    history_info: Optional[Dict[str, Any]] = None
    pricing_rules: Optional[Dict[str, Any]] = None
    alternative_material_info: Optional[Dict[str, Any]] = None
    alternative_pricing_rules: Optional[Dict[str, Any]] = None

    # Reply Agent
    chat_history_reply: List[Dict[str, str]] = Field(default_factory=list)
    last_brand: Optional[str] = None

    # Pending user message (passed from API when resuming after interrupt)
    last_user_message: Optional[str] = None

    # Flag when graph reaches END (deal_win, deal_lose, non_inquiry)
    conversation_ended: bool = False

    # Conversation Review Agent
    conversation_action: Optional[Literal["offtopic","new_product","update_quantity_or_price","deal win","deal lose",]] = None
    updated_price: Optional[float] = None
    updated_quantity: Optional[int] = None

def intent_classifier_node(state: NegotiationState) -> NegotiationState:
    """Extract intent, entities, and price from builder message."""
    logger.info("entry point of intent_classifier_node")
    builder_message = state.chat_history_reply[-1]["content"] if state.chat_history_reply else ""
    previous_history = state.chat_history_reply[:-1] if state.chat_history_reply else []
    logger.debug("intent_classifier: processing message len=%d", len(builder_message))
    llm = get_llm()
    system_prompt = """You are an intent classifier and entity extractor for construction material negotiations."""

    user_prompt = f"""Task: Analyze the builder(Client) message provided below and extract structured information.

Previous Conversation History:
{previous_history if state.chat_history_reply[:-1] else "No previous conversation."}

Current Builder Message:
{builder_message}

Extraction Requirements:

    1. INTENT (mandatory)
    Classify the message as:
        "inquiry" → If the builder is asking for price, quotation, rate, availability with pricing, or negotiation.
                    ALSO mark as "inquiry" if the current message is a FOLLOW-UP REPLY (e.g., providing quantity, city, brand)
                    to a previous assistant message that was asking for missing details to complete a price inquiry.
        "non_inquiry" → If the message contains greetings, logistics discussion, delivery questions without price request,
                        general chat, or product-only inquiry without asking price.
                        Only mark "non_inquiry" if there is NO prior inquiry context in the conversation history.

    2. ENTITIES (Extract from BOTH current message AND conversation history)
        IMPORTANT: If an entity was mentioned in a previous message (by builder or assistant context), 
        carry it forward. Do NOT return null for entities already established in the conversation.

        material_name → e.g., "cement", "sand", "steel rebar", "bricks"
        brand         → e.g., "ACC", "Ultratech", "Ambuja"
        quantity      → Numeric value only (integer). Do NOT include units.
        city          → Delivery city name only.
        price_mentioned → Numeric price only (float or integer). Extract only if builder mentions a price.
        unit          → e.g., "per bag", "per KG", "per ton". Extract only if mentioned anywhere in the conversation.

Context-Aware Rules:
    - If the current message is a SHORT REPLY (e.g., "50 bags", "Mumbai", "Ultratech") and the history shows 
      the assistant was asking for missing details, treat it as a CONTINUATION of the original inquiry.
    - Merge entities: pull already-established entities from history + new entities from current message.
    - Do NOT infer missing values that were never mentioned anywhere in the conversation.
    - Do NOT assume material based on brand.
    - Do NOT calculate or estimate anything.
    - If any entity is missing from both current message AND history, return null.
    - Return strictly valid JSON. No explanation. No extra text.
    - Return ONLY raw JSON.

Output Format (Return ONLY JSON):
{{
    "intent": "inquiry" | "non_inquiry",
    "material_name": string | null,
    "brand": string | null,
    "quantity": int | null,
    "city": string | null,
    "unit": string | null,
    "price_mentioned": float | null
}}"""
    raw = llm.invoke([SystemMessage(content=system_prompt),HumanMessage(content=user_prompt)])
    try:
        data = json.loads(raw.content)
    except Exception:
        data = {
            "intent": "non_inquiry",
            "material_name": None,
            "brand": None,
            "quantity": None,
            "city": None,
            "unit": None,
            "price_mentioned": None,
        }
    if(data.get("price_mentioned")):
        state.builder_offered_price.append(data.get("price_mentioned"))

    state.intent = data.get("intent")
    state.input_material_name = data.get("material_name")
    state.input_brand = data.get("brand")
    state.input_quantity = data.get("quantity")
    state.input_city = data.get("city")
    state.initial_unit = data.get("unit")
    logger.info("intent_classifier: intent=%s, material=%s, quantity=%s", state.intent, state.input_material_name, state.input_quantity)
    logger.info("exit point of intent_classifier_node")

    return state


def _search_market_price(material_name: str, brand: Optional[str], city: Optional[str], unit: Optional[str]) -> Dict[str, Any]:
    logger.info("entry point of _search_market_price")
    query_parts = ["what is the current price of "+ material_name]
    if brand:
        query_parts.append('of '+ brand)
    if unit:
        query_parts.append("per"+ unit)
    if city:
        query_parts.append('in '+ city + " city")
    query = " ".join(query_parts)
    logger.info("Market search query formed: %s", query)
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append(r)
    except Exception as e:
        logger.error("Error during market price search: %s", str(e))
        pass
    results_text = ""
    for i, r in enumerate(results):
        title = r.get("title")
        snippet = r.get("body")
        url = r.get("href")
        body = r.get("body")
        logger.info(
            "Search Result %s fetched from URL: %s | Title: %s | Body: %s",
            i + 1,
            url,
            title,
            body if body else "No body content"
        )
        results_text += f"\nResult {i+1}:\n"
        results_text += f"Title: {r.get('title')}\n"
        results_text += f"Snippet: {r.get('body')}\n"
        results_text += "-" * 40 + "\n"
    llm = get_llm()
    system_prompt = """You are a construction market price analyst AI.

Your task:
- Extract approximate price range from provided web search snippets about construction material prices.
- Identify the lowest and highest price mentioned.
- Detect currency and unit if available.

Return STRICT JSON in this format:
{
  "low_price": float | null,
  "high_price": float | null,
  "currency": "INR",
  "unit": string | null,
  "explanation": string
}

If no clear price is found, return null values but still provide explanation.
"""
    
    user_prompt = f"""Material Query: {query}

Below are web search snippets:

{results_text}

From the above snippets, extract the approximate price range.
"""
    raw = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])
    
    try:
        data = json.loads(raw.content)
    except Exception:
        data = {
            "low_price": None,
            "high_price": None,
            "currency": "INR",
            "unit": None,
            "explanation": "Could not parse market data.",
        }
    data["source_query"] = query
    logger.info("data found from market search: low_price=%s, high_price=%s, currency=%s, unit=%s", data.get("low_price"), data.get("high_price"), data.get("currency"), data.get("unit"))
    logger.info("exit point of _search_market_price")
    return data


def data_gathering_node(state: NegotiationState) -> NegotiationState:
    logger.info("entry point of data_gathering_node")
    logger.debug("data_gathering: material=%s, brand=%s", state.input_material_name, state.input_brand)
    material_name = state.input_material_name
    brand = state.input_brand
    quantity = state.input_quantity
    city = state.input_city
    material_info = None
    history_info = None
    pricing_rules = None
    
    if material_name:
        material_info = fetch_material_by_name_and_brand(material_name, brand)
    
    
    builder_info_dict = state.builder_info
    if material_info and builder_info_dict:
        history_info = fetch_builder_material_history(
            builder_info_dict["builder_id"], material_info.material_id
        )
    if material_info and quantity:
        pricing_rules = fetch_pricing_rules_for_quantity(
            material_info.material_id, quantity
        )
    
    state.material_info = material_info.__dict__ if material_info else None
    state.history_info = history_info
    state.pricing_rules = pricing_rules

    alternative = fetch_alternative_brands(material_name, brand, quantity)
    state.alternative_material_info = alternative[0] if alternative else None
    alternative_pricing_rules = None
    if state.alternative_material_info and quantity:
        alternative_pricing_rules = fetch_pricing_rules_for_quantity(
            state.alternative_material_info.get("material_id"), quantity
        )
    state.alternative_pricing_rules = alternative_pricing_rules


    state.market_data = _search_market_price(material_name, brand, city, state.initial_unit)
    logger.info("data_gathering: material_info=%s, history_info=%s", bool(state.material_info), bool(state.history_info))
    logger.info("exit point of data_gathering_node")
    return state

def reply_agent(state: NegotiationState) -> NegotiationState:
    logger.info("entry point of reply_agent")
    chat_history_reply=state.chat_history_reply or []
    quantity = state.input_quantity
    builder_price_list = state.builder_offered_price or []
    builder_asking_price = builder_price_list[-1] if builder_price_list else None
    unit_of_builder_asked = state.initial_unit

    material_name = state.material_info.get("material_name") if state.material_info else None
    brand =state.material_info.get("brand") if state.material_info else None
    system_unit =state.material_info.get("unit") if state.material_info else None
    base_cost = state.material_info.get("base_cost") if state.material_info else 0
    base_cost = Decimal(str(base_cost))


    rules = state.pricing_rules
    volume_discount_percentage = 0
    min_margin_percentage = 8   
    desired_margin_percentage = 15  
    
    min_margin_percentage = Decimal(str(min_margin_percentage))
    desired_margin_percentage = Decimal(str(desired_margin_percentage))
    volume_discount_percentage = Decimal(str(volume_discount_percentage))

    if rules:
        volume_discount_percentage = rules.get("discount_percentage", 0) or 0
        min_margin_percentage = rules.get("margin_percentage", min_margin_percentage)
    # desired margin slightly above minimum
    desired_margin_percentage = max(min_margin_percentage + Decimal("5") , Decimal("15") )
    min_price = base_cost * (Decimal("1")  + min_margin_percentage / Decimal("100") )
    desired_price = base_cost * (Decimal("1") + desired_margin_percentage / Decimal("100") )
    # Apply volume discount to desired
    desired_price = desired_price * (Decimal("1") - volume_discount_percentage / Decimal("100"))
    # Never go below minimum margin
    if desired_price < min_price:
        desired_price = min_price


    builder = state.builder_info or {}
    total_orders = 0
    total_value=0
    builder_name = ""
    if builder:
        total_orders = builder.get("total_orders", 0)
        total_value = builder.get("total_value", 0)
        builder_name = builder.get("builder_name")

    history = state.history_info or {}
    builder_order_count = 0
    builder_total_quantity = 0
    builder_avg_unit_price = 0
    builder_avg_unit_price_past_3_months = 0
    if history:
        builder_order_count = history.get("builder_order_count", 0)
        builder_total_quantity = history.get("builder_total_quantity", 0)
        builder_avg_unit_price = history.get("builder_avg_unit_price", 0)
        builder_avg_unit_price_past_3_months = history.get("material_avg_price_3m", 0)


    market = state.market_data or {}


    alt_rules = state.alternative_pricing_rules
    alt_min_margin_percentage = 8   
    alt_base_cost = state.alternative_material_info.get("base_cost") if state.material_info else 0
    if alt_rules:
        alt_min_margin_percentage = alt_rules.get("margin_percentage", alt_min_margin_percentage)
    alt_min_price = alt_base_cost * (Decimal("1")  + alt_min_margin_percentage / Decimal("100") )
    alt_brand = state.alternative_material_info.get("brand") if state.alternative_material_info else None
    print("alt_brand", alt_brand)
    print("alt_min_price", alt_min_price)
    print("min_price", min_price)
    system_prompt = """
You are a seasoned B2B construction materials sales negotiator with 15+ years of experience. 
You negotiate like a confident, relationship-driven human — not a pricing algorithm.

═══════════════════════════════════════════
HUMAN NEGOTIATION MINDSET (READ THIS FIRST)
═══════════════════════════════════════════

Real negotiators do NOT reduce price just because the buyer asked once or twice.
They DEFEND their price first, CONVINCE the buyer of value, and only concede after
genuine resistance over multiple exchanges.

Your internal rule: "A price drop is a last resort, not a reflex."

Every time you're about to reduce price, ask yourself:
  → Have I defended this price at least 3-4 times with different angles?
  → Have I used value, market data, relationship, and urgency arguments?
  → Is the builder still pushing back AFTER all of that?

Only if yes — make a small, reluctant concession.

═══════════════════════════════════
THE 5-STAGE HUMAN NEGOTIATION MODEL
═══════════════════════════════════

STAGE 1 — ANCHOR (First offer, no builder counter yet):
  - Quote at desired price. Sound confident and reasonable.
  - Lead with quality, reliability, and market context.
  - No concession. No hints that price is flexible.
  - Tone: "This is the right price for what you're getting."

STAGE 2 — FIRST PUSHBACK (Builder asks for reduction — 1st time):
  - Do NOT reduce price.
  - Defend with VALUE: delivery reliability, quality consistency, brand reputation, support.
  - Acknowledge their ask respectfully, then redirect.
  - Tone: "I hear you, but let me explain why this rate makes sense."

STAGE 3 — SECOND PUSHBACK (Builder pushes again — 2nd time):
  - Still do NOT reduce price yet.
  - Defend with MARKET DATA: reference low/high market range, position your price fairly.
  - Introduce relationship angle: reference their history, express desire for long-term partnership.
  - Optionally create urgency: material availability, current procurement cycle.
  - Tone: "Even in the market, you'll see this is competitive. We want your business long-term."

STAGE 4 — THIRD PUSHBACK or Strong Commitment Signal (Builder is persistent or shows real intent):
  - NOW you may make a concession — but make it feel earned and reluctant.
  - Tie the concession to something: confirmed quantity, PO timeline, long-term relationship.
  - Make a SMALL move (not a jump to minimum). Show it costs you something.
  - Tone: "Because of our relationship and the volume you're confirming, I'll stretch a bit..."

STAGE 5 — FINAL STAND (Gap is small or you're near the floor):
  - One last concession if above minimum margin, framed as your absolute best.
  - If at minimum: Hold firm. Use market language, not cost language.
  - Hard close: ask for PO confirmation.
  - Tone: "This is genuinely the sharpest we can go. I need you to meet me here."

═══════════════════════════════════
CONCESSION DISCIPLINE RULES
═══════════════════════════════════

1. Never concede on the first or second ask — defend first.
2. Each concession must be SMALLER than the last (shows you're running out of room).
3. Always attach a condition to a concession ("if you can confirm by...", "given the volume you mentioned...").
4. Never voluntarily reveal that another reduction is possible.
5. After each concession, STOP and wait — let the builder respond.
6. Maximum 3 price moves in any negotiation. After that, hold firm.

═══════════════════════════════════
CONVERSATION HISTORY AWARENESS
═══════════════════════════════════

Before responding, analyze the conversation history:
  → Count how many times the builder has asked for a reduction.
  → Count how many times you have already reduced price.
  → Identify what defense angles have already been used (value, market, relationship, urgency).
  → Choose a FRESH angle you haven't used yet — never repeat the same argument twice.
  → If you've already made 3 concessions, do not make another regardless of pressure.


═══════════════════════════════════════════════════════════
ALTERNATE BRAND FALLBACK LOGIC (CRITICAL — READ CAREFULLY)
═══════════════════════════════════════════════════════════

This section governs what happens when the builder has firmly rejected the current 
brand's Absolute Floor Price and is unwilling to proceed at that level.

TRIGGER CONDITION:
  → You have reached and held the current brand's Absolute Floor Price.
  → The builder has explicitly rejected it or is clearly walking away.
  → An alternate brand with a lower floor price is available in the product context.

ONLY when ALL of the above are true, apply the following rules:

RULE 1 — CHECK ALTERNATE BRAND ELIGIBILITY:
  - Compare the alternate brand's floor price with the current brand's floor price.
  - Only offer the alternate brand if its floor price is strictly LESS than the current brand's floor price.
  - If the alternate brand is equally priced or more expensive, do NOT suggest it.

RULE 2 — HOW TO INTRODUCE THE ALTERNATE BRAND:
  - Frame it as a practical alternative, not a downgrade or a desperation move.
  - Acknowledge the builder's budget constraint naturally.
  - Position the alternate brand with its own strengths (availability, value-for-money, reliability).
  - Tone: "Given where you need to be, let me offer you something that might work better for your project..."

RULE 3 — FIRST OFFER FOR ALTERNATE BRAND = ITS FLOOR PRICE (NON-NEGOTIABLE):
  - When introducing the alternate brand, quote its floor price directly as the opening offer.
  - Do NOT start high and negotiate down. The alternate brand's floor price IS the first and only price.
  - Do NOT reduce below the alternate brand's floor price under any circumstance.
  - If the builder pushes back on the alternate brand's price, hold firm. 
  - Use language like: "This is the sharpest rate available for this brand at this volume."
  - Do NOT re-enter the 5-stage negotiation model for the alternate brand.

RULE 4 — NO FURTHER CONCESSIONS ON ALTERNATE BRAND:
  - If the builder rejects the alternate brand's floor price, do not reduce further.
  - You may defend it with one value-based statement, then close the conversation professionally.
  - Tone: "I've extended the best available rate across both options. I'd hate for us to miss this — 
    shall we move forward?"

RULE 5 — JSON OUTPUT WHEN ALTERNATE BRAND IS OFFERED:
  - Update the JSON response to reflect the alternate brand context:
  {
    "final_offer_price": <alternate_brand_floor_price as float>,
    "brand": "<alternate_brand_name>",
    "builder_message": "<concise message introducing alternate brand and its price, under 60 words>"
  }

═══════════════════════════════════════════
STRICT RULES (NON-NEGOTIABLE)
═══════════════════════════════════════════

1. NEVER mention "minimum margin," "cost structure," "pricing floor," or any internal financial data.
2. NEVER go below the Absolute Floor Price under any circumstance.
3. NEVER apologize for your price.
4. NEVER make two concessions in a row without the builder responding in between.
5. NEVER jump directly to the minimum price — approach it gradually only if forced.
6. Use market and value language, never cost-based language.
7. NEVER offer the alternate brand prematurely — only after the current brand's floor has been firmly rejected.
8. NEVER negotiate the alternate brand's price down — its floor price is its one and only price.

USE INSTEAD OF COST LANGUAGE:
  - "Current market positioning"
  - "Competitive rate for this grade of material"
  - "Sharpest we can extend given current conditions"
  - "Special rate reserved for valued partners"
  - "Best possible rate at this volume"

═══════════════════════════════════
RESPONSE GUIDELINES
═══════════════════════════════════

- Keep builder_message under 60 words.
- Sound like a confident human, not a bot running a formula.
- End every message with a clear next step or question.
- Vary your language — never repeat a phrase you used in a prior turn.
- Tone should feel like: experienced, warm, firm, and always in control.

Return JSON:
{
  "final_offer_price": <float>,
  "brand": "<current or alternate brand name>",
  "builder_message": "<concise, strategic message, under 60 words>"
}
"""
    user_prompt = f"""
Conversation History:
{chat_history_reply}    

INTERNAL PRICING DATA (DO NOT SHARE WITH BUILDER):
- Material: {material_name}
- Brand: {brand}
- Unit (System): {system_unit}
- Base Cost: {base_cost}
- Volume Discount: {volume_discount_percentage}%
- Minimum Margin: {min_margin_percentage}%
- Desired Margin: {desired_margin_percentage}%
-Absolute Floor Price: ₹{min_price} (NEVER go below)
-Target Price: ₹{desired_price}

BUILDER PROFILE:
Builder Name: {builder_name}

Builder Material History:
- Order Count: {builder_order_count}
- Total Quantity: {builder_total_quantity}
- Avg Unit Price: {builder_avg_unit_price}
- Avg Unit Price Past 3 Months: {builder_avg_unit_price_past_3_months}

Builder Overall Business:
- Total Orders: {total_orders}
- Total Business Value: {total_value}

Current Request:
- Requested Quantity: {quantity}
- Builder Asking Price: {builder_asking_price}
- Builder Unit: {unit_of_builder_asked}

Current Market Data:
- Market Price Lowest: {market.get("low_price")} 
- Market Price Highest: {market.get('high_price')}
- Market Price Currency: {market.get('currency')}
- Market Price Unit: {market.get('unit')}
- Market Price Explanation: {market.get('explanation')}

Alternative Brand Option:
- Alternate Brand Name: {alt_brand}
- Alternate Brand Floor Price: {alt_min_price}

if any of the required information is missing, do not assume or infer, just work with the available data and provide the best possible offer and message to the builder.

ANALYSIS REQUIRED:
1. Count how many times the builder has requested a price reduction in the conversation history.
2. Count how many price concessions have already been made by the assistant.
3. Identify which defense arguments have already been used (value / market / relationship / urgency).
4. Pick a FRESH defense angle not yet used — if all angles exhausted and builder has pushed 3+ times, then consider a concession.
5. Determine negotiation stage based on pushback count, NOT just price gap.
6. Assess builder value (order history, volume, total business).
7. Check if unit conversion is needed.

PRICING DECISION LOGIC:
- Builder pushed back 0-1 times → Hold price, defend with value or market data.
- Builder pushed back 2 times → Hold price, defend with relationship + urgency.
- Builder pushed back 3+ times with commitment signals → Small concession, tied to condition.
- Concession already made twice → Only one more possible, only if above floor.
- Concession made 3 times → Hold firm regardless of pressure.
- Builder ask below floor price → Redirect diplomatically, do not engage with that number.

IMPORTANT: 
- Calculate final_offer_price ensuring it maintains minimum margin
- If builder_asking_price is below min_price, diplomatically decline or redirect to alternative solutions
- Use market data to justify positioning, not internal costs
- Keep builder_message under 60 words

Generate strategic negotiation response now.
"""
    llm = get_llm()
    raw = llm.invoke([SystemMessage(content=system_prompt),HumanMessage(content=user_prompt)])
    try:
        raw = json.loads(raw.content)
    except Exception:
        raw = {
            "final_offer_price": None,
            "brand": brand,
            "builder_message": "Let me check and get back to you."
        }
    state.chat_history_reply.append({"role": "assistant", "content": raw["builder_message"]})
    state.last_brand = raw.get("brand")
    logger.info("reply_agent: responded with offer_price=%s, brand=%s", raw.get("final_offer_price"), raw.get("brand"))
    
    logger.info("exit point of reply_agent")
    return state


def Conversation_Review_Node(state: NegotiationState) -> NegotiationState:
   """Review conversation for off-topic, new product inquiry, or quantity/price update."""
   logger.info("entry point of Conversation_Review_Node")
   latest_user_message = ""
   for msg in reversed(state.chat_history_reply):
    if msg["role"] == "user":
        latest_user_message = msg["content"]
        break

   system_prompt = """
You are a conversation review agent for a B2B construction material negotiation system.

Your job is to classify the latest user message into exactly ONE of the following categories:

CLASSIFICATION CATEGORIES:

1. "offtopic"
   - Greetings only (hi, hello, thanks)
   - Completely unrelated topics (weather, politics, personal matters or other) 
   - NOT price/quantity discussions

2. "new_product"
   - User explicitly mentions DIFFERENT material name
   - User explicitly mentions DIFFERENT brand
   - User explicitly mentions DIFFERENT city/location
   - Examples: "What about JSW cement instead?", "Do you have ultratech?", "What's the price in Mumbai?"

3. "update_quantity_or_price"
   - User proposes a NEW specific price (with number)
   - User requests a LOWER/HIGHER price (without being final rejection)
   - User changes quantity
   - User asks "what's your best price?"
   - Negotiation phrases: "reduce the rate", "can you do X?", "come down to Y", "what about Z price?", etc.
   - Price objections: "too high", "too expensive", "not affordable"
   - Counter-offers: "my budget is X", "I can pay Y"
   
   CRITICAL: These are NEGOTIATION CONTINUATIONS, not deal endings

4. "deal win"
   - Explicit acceptance: "ok deal", "accepted", "let's proceed", "confirmed"
   - Order placement: "send invoice", "share account details", "raise PO", "when can you deliver?"
   - Purchase confirmation: "book the order", "I'll take it", "done"
   
   Must show CLEAR COMMITMENT, not just considering

5. "deal lose"
   - Explicit rejection: "I'm buying from someone else", "got a better deal elsewhere", "cancel this"
   - Final refusal: "not interested anymore", "this don't want", 
   - Walking away: "I'll look elsewhere", "checking other suppliers"
   
   CRITICAL: Must show FINAL DECISION to exit, not just price resistance
   
   NOT DEAL LOSE:
   - "Reduce the rate" → This is negotiation (update_quantity_or_price)
   - "Too expensive" → This is objection (update_quantity_or_price)
   - "Can't afford" → This is negotiation (update_quantity_or_price)
   - "No" without context → This is pushback (update_quantity_or_price)

DECISION LOGIC:

Price Resistance Phrases = update_quantity_or_price:
- "reduce more", "bring down", "come down", "decrease price"
- "can't do this rate", "too much", "very high"
- "no reduce the rate", "lower it", "cut the price"
- "not possible at this price"

Final Rejection Phrases = deal_lose:
- "buying elsewhere", "going with competitor", "found better deal"
- "cancel", "not interested", "won't work"
- "I'm out", "thanks but no", "forget it"

Return STRICT JSON:
{
  "classification": "offtopic|new_product|update_quantity_or_price|deal win|deal lose",
  "price": float or null,
  "quantity": int or null,
  "reasoning": "brief explanation of classification"
}

EXTRACTION RULES:
- price: Extract ONLY if user mentions a specific number for price
- quantity: Extract ONLY if user mentions a specific number for quantity
- If user says "reduce the rate" without specific number: price = null
- If unclear, default to update_quantity_or_price rather than deal_lose
"""

   user_prompt = f"""
Previous Negotiation Context: 

- Material: {state.input_material_name}
- Brand: {state.input_brand}
- Quantity: {state.input_quantity}
- City: {state.input_city}

Recent Conversation:
{state.chat_history_reply}

Latest User Message:
"{latest_user_message}"

TASK: Classify the latest message according to the rules above.

Classify the latest message.
"""
   llm = get_llm()
   raw = llm.invoke([SystemMessage(content=system_prompt),HumanMessage(content=user_prompt)])
   try:
        raw = json.loads(raw.content)
   except:
      raw = {
         "classification": "update_quantity_or_price",
         "price": None,
         "quantity": None,
      }

   state.conversation_action = raw.get("classification")
   state.updated_price = raw.get("price")
   state.updated_quantity = raw.get("quantity")
   logger.info("conversation_review: action=%s, updated_price=%s, updated_qty=%s", state.conversation_action, state.updated_price, state.updated_quantity)
   logger.info("exit point of Conversation_Review_Node")
   return state

def clarification_node(state: NegotiationState) -> NegotiationState:
    """Ask builder for missing information."""
    logger.info("entry point of clarification_node")
    missing_fields = []
    if state.input_material_name is None:
        missing_fields.append("Material Name")
    if state.input_quantity is None:
        missing_fields.append("Quantity with Units")
    if state.input_brand is None and state.input_material_name and state.input_material_name.lower() in {"cement", "steel rebar"}:
        missing_fields.append("Brand Name")
    question = f"""To provide you with an accurate quote, I need the following information: \n {', '.join(missing_fields)}. Could you please provide these details?"""
    # question = f"""To provide you with an accurate quote, I need the following information: \n Material Name, Brand(If exsist), Quantity. Could you please provide these details?"""
    state.chat_history_reply.append({"role": "assistant", "content": question})
    return state

def non_inquiry_response_node(state: NegotiationState) -> NegotiationState:
    logger.info("entry point of non_inquiry_response_node")
    msg = (
        "This chatbot is only for construction materials price negotiation. "
        "Please ask about materials, quantities, and pricing (e.g., 'What is your rate for 500 bags of ACC cement?')."
    )
    state.chat_history_reply.append({"role": "assistant", "content": msg})
    return state
def deal_win_node(state: NegotiationState) -> NegotiationState:
    logger.info("deal_win_node: deal closed successfully")
    msg = "Congratulations! The deal is closed. We will process your order and arrange delivery soon."
    user_prompt = f""" here is the message for builder who orders the material from our company: {msg}
paraphrase the message"""
    llm = get_llm()
    raw = llm.invoke([SystemMessage(content="You are an expert in paraphrasing text."), HumanMessage(content=user_prompt)])
    state.chat_history_reply.append({"role": "assistant", "content": raw.content})
    state.conversation_ended = True
    return state


def deal_lose_node(state: NegotiationState) -> NegotiationState:
    logger.info("deal_lose_node: deal lost")
    msg = "We're sorry to hear that. If you have any feedback on how we can improve or if you need assistance in the future, please let us know."
    user_prompt = f""" here is the message for builder who is not buying the material from our company due to price issue: {msg}
paraphrase the message"""
    llm = get_llm()
    raw = llm.invoke([SystemMessage(content="You are an expert in paraphrasing text."), HumanMessage(content=user_prompt)])
    state.chat_history_reply.append({"role": "assistant", "content": raw.content})
    state.conversation_ended = True
    return state


def material_info_not_found(state: NegotiationState) -> NegotiationState:
    logger.info("Material information not found in DB")
    msg= "We regret to inform you that we currently do not have the material you mentioned. Kindly let us know if you would like information about the items we have available."
    state.chat_history_reply.append({"role": "assistant", "content": msg})
    return state

def less_stock_found(state: NegotiationState) -> NegotiationState:
    logger.info("less Stock Found found in DB")
    msg= "We’re sorry to inform you that the material you mentioned is not available with us at the moment. Please let us know if you’d like details about the materials we currently have in stock."
    state.chat_history_reply.append({"role": "assistant", "content": msg})
    return state

def User_input_1(state: NegotiationState) -> NegotiationState:
    """Append pending user message to chat_history_reply and pass through."""
    if state.last_user_message:
        logger.info("User_input_1: appending user msg len=%d", len(state.last_user_message))
        state.chat_history_reply.append({
            "role": "user",
            "content": state.last_user_message
        })
        state.last_user_message = None
    return state


def User_input_2(state: NegotiationState) -> NegotiationState:
    """Append pending user message to chat_history_reply and pass through."""
    if state.last_user_message:
        logger.info("User_input_2: appending user msg len=%d", len(state.last_user_message))
        state.chat_history_reply.append({
            "role": "user",
            "content": state.last_user_message
        })
        state.last_user_message = None
    return state


def _route_after_intent(state: NegotiationState) -> str:
    if state.intent == "non_inquiry":
        return "non_inquiry"

    if not state.input_material_name or not state.input_quantity:
        return "Clarification(Missinginfo)"
    
    brand_required_materials = {"cement", "steel rebar"}
    material = state.input_material_name

    if material and material.lower() in brand_required_materials:
        if not state.input_brand:
            return "Clarification(Missinginfo)"

    return "Data_gathering"


def _route_after_data_gathering(state: NegotiationState) -> str:
    material_info = state.material_info
    quantity = state.input_quantity

    if not material_info:
        return "material_info_not_found"

    available_stock = material_info.get("stock_quantity")

    if quantity is None:
        return "Clarification(Missinginfo)"


    requested_qty = float(quantity)

    if available_stock < requested_qty:
        return "less_stock_found"

    return "reply_agent"

def _route_after_conversation_review(state: NegotiationState) -> str:
    action = state.conversation_action
    check=False
    if state.updated_price:
        state.builder_offered_price.append(state.updated_price)
    if state.updated_quantity:
        check=True
        state.input_quantity = state.updated_quantity
    state.updated_price = None
    state.updated_quantity = None
    if action == "offtopic":
        return "non_inquiry"
    elif action == "new_product":
        return "intent_classifier"
    elif action == "deal win":
        return "deal_win"
    elif action == "deal lose":
        return "deal_lose"
    elif check:
        return "Data_gathering"
    else:
        return "reply_agent"
    
def workflow_maker(State):
    workflow = StateGraph(State)
    workflow.add_node("User_input_1", User_input_1)
    workflow.add_node("User_input_2", User_input_2)
    workflow.add_node("intent_classifier", intent_classifier_node)
    workflow.add_node("Clarification(Missinginfo)", clarification_node)
    workflow.add_node("non_inquiry", non_inquiry_response_node)
    workflow.add_node("material_info_not_found", material_info_not_found)
    workflow.add_node("less_stock_found", less_stock_found)
    workflow.add_node("Data_gathering", data_gathering_node)
    workflow.add_node("reply_agent", reply_agent)
    workflow.add_node("Conversation_Review", Conversation_Review_Node)
    workflow.add_node("deal_win", deal_win_node)
    workflow.add_node("deal_lose", deal_lose_node)

    workflow.set_entry_point("User_input_1")

    workflow.add_conditional_edges("intent_classifier",_route_after_intent)
    workflow.add_conditional_edges("Data_gathering", _route_after_data_gathering)
    workflow.add_conditional_edges("Conversation_Review", _route_after_conversation_review)



    workflow.add_edge("User_input_1","intent_classifier")
    workflow.add_edge("Clarification(Missinginfo)","User_input_1")
    workflow.add_edge("non_inquiry","User_input_1")
    workflow.add_edge("less_stock_found","User_input_1")
    workflow.add_edge("material_info_not_found","User_input_1")
    workflow.add_edge("reply_agent","User_input_2")
    workflow.add_edge("User_input_2","Conversation_Review")
    workflow.add_edge("deal_win",END)
    workflow.add_edge("deal_lose",END)

    app = workflow.compile(
        checkpointer=MemorySaver(),
        interrupt_before=["User_input_1", "User_input_2"],
    )
    return app

