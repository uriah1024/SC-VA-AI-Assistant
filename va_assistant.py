import os
import hashlib
import re
import json
import random
from flask import Flask, request, jsonify
from elevenlabs.client import ElevenLabs
import google.generativeai as genai

# ==========================================
#              CONFIGURATION
# ==========================================

# 1. PATHS
BASE_LIBRARY_PATH = r"C:\Star Citizen Voice Library"
SHIPS_LIBRARY_PATH = os.path.join(BASE_LIBRARY_PATH, "Ships")
STANDARD_LIBRARY_PATH = os.path.join(BASE_LIBRARY_PATH, "Standard")
SHIPS_DB_PATH = os.path.join(BASE_LIBRARY_PATH, "ships.json")
TACTICAL_DB_PATH = os.path.join(BASE_LIBRARY_PATH, "ship_tactical_analysis.json")

# 2. API KEYS (Paste yours here)
ELEVENLABS_API_KEY = "sk_a42024267b4b5764d2d393512b6ab3a22ff5c58bf0a6948e"
GEMINI_API_KEY = "AIzaSyAnrnIznnvyZgESSRzeQ6dPfLV-wEJAV1A"

# 3. SETTINGS
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George
CREDIT_LOW_THRESHOLD = 1000   # Stop generating if total credits < 1000
SESSION_CHAR_LIMIT = 500      # Stop generating if session uses > 500 chars
INTRO_FATIGUE_LIMIT = 6
USER_SHIP_NAME = "hawk"  # Your current ship

# 4. VOICE MAP
VOICE_MAP = {
    "EXAVITQu4vr4xnSDxMaL": "Sarah",
    "JBFqnCBsd6RMkjVDRZzb": "George"
}

# ==========================================
#           STATE MANAGEMENT
# ==========================================

session_chars_used = 0
chat_history = []
session_counters = {}  # Tracks how many times we've analyzed a specific ship this session. Ex.: Structure: { "gladius": 2, "vanguard": 5 }
insight_queues = {}    # Tracks deep dive points so we don't repeat. Ex.Structure: { "gladius": ["Enemy has better pitch", "You have EMP"] }
active_target = None  # <--- NEW: Remembers the last ship we discussed


# Initialize APIs
print("Initializing APIs...")
app = Flask(__name__)
client_el = ElevenLabs(api_key=ELEVENLABS_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)   # We use 'gemini-1.5-flash' because it is fast and cheap.

model = genai.GenerativeModel('gemini-2.5-flash-lite')
    # 'gemini-2.5-flash'
    # generation_config={"response_mime_type": "application/json"}   # We force 'application/json' so it never replies with conversational text.

# ==========================================
#           HELPER FUNCTIONS
# ==========================================

def load_json(path):
    try:
        with open(path, 'r') as f: return json.load(f)
    except: return {}

def check_account_health():
    """Checks actual ElevenLabs balance on startup."""
    try:
        sub = client_el.user.subscription.get()
        remaining = sub.character_limit - sub.character_count
        print(f"💰 CREDIT CHECK: {remaining} characters remaining.")
        return remaining
    except Exception as e:
        print(f"⚠️ Could not verify credits: {e}")
        return 99999

def can_afford_generation():
    global session_chars_used
    if session_chars_used > SESSION_CHAR_LIMIT:
        print("🛑 SESSION LIMIT REACHED. Switching to Static Mode.")
        return False
    return True

def get_storage_path(voice_id, ship_name=None, subfolder=None):
    """Determines the exact folder to save/load audio."""
    if ship_name:
        folder_name = ship_name.title()
        full_path = os.path.join(SHIPS_LIBRARY_PATH, folder_name)
        if subfolder:
            full_path = os.path.join(full_path, subfolder)
    else:
        voice_name = VOICE_MAP.get(voice_id, "Unknown")
        full_path = os.path.join(BASE_LIBRARY_PATH, voice_name)

    if not os.path.exists(full_path):
        os.makedirs(full_path)
    return full_path

def get_cached_files(voice_id, ship_name=None, subfolder=None, tag="generic"):
    """Finds existing MP3s matching the criteria."""
    search_folder = get_storage_path(voice_id, ship_name, subfolder)
    voice_name = VOICE_MAP.get(voice_id, "unknown").lower()
    
    if not os.path.exists(search_folder): return []
    
    # Filter by tag AND voice name
    files = [
        f for f in os.listdir(search_folder) 
        if tag.lower() in f.lower() 
        and voice_name in f.lower()
    ]
    return [os.path.join(search_folder, f) for f in files]

def clean_text_for_speech(text):
    """
    Sanitizes Star Citizen jargon into natural spoken English.
    """
    # --- UNITS & MATH ---
    text = text.replace("+", "plus ")
    text = text.replace(" -", " minus ") 
    text = text.replace("%", " percent")
    # Fix "m/s" first so it doesn't get messed up by single "m" checks
    text = text.replace(" km/s", " kilometers per second") 
    text = text.replace(" m/s", " meters per second")
    text = text.replace(" km", " clicks") # Or use "klicks" for military flavor
    text = text.replace(" m ", " meters")
    text = text.replace(" min ", " mikes")  # Military slang for minutes
    text = text.replace(" deg ", " degrees")
    
    # --- COMPONENT SIZES ---
    # "S3" -> "Size 3"
    text = re.sub(r"\bS(\d)\b", r"Size \1", text) # Matches S1, S2, S3, etc.
    
    # --- STAR CITIZEN JARGON ---
    # text = text.replace("HP", "structural integrity") # "Hit points" sounds gamey. "Integrity" sounds immersive.
    text = text.replace("aUEC", "credits")
    text = text.replace("UEC", "credits")
    text = text.replace("SCU ", "S C U") # Force letter pronunciation
    text = text.replace("SCM", "S C M") # Force letter pronunciation
    text = text.replace("PDC", "P D C") # Force letter pronunciation
    text = text.replace("PDT", "P D T") # Force letter pronunciation
    text = text.replace("NAV ", "Nav") # Short for navigation
    text = text.replace(" QD ", "Quantum Drive")
    text = text.replace(" QT ", "Quantum Travel")
    text = text.replace(" EM ", "Electro-magnetic")
    text = text.replace(" IR ", "Infra-red")
    text = text.replace(" CS ", "Cross-section")

    # --- SHIP NAMES ---
    text = text.replace("MDC", "M D C") # Force letter pronunciation
    text = text.replace("MTC", "M T C") # Force letter pronunciation
    text = text.replace("Carrack", "Car-rack")
    text = text.replace("Tumbril", "Tum-bril")
    text = text.replace("Valkyrie", "Val-ky-ree")
    text = text.replace("Shubin", "Shoo-bin")
    text = text.replace("Ursa Medivac", "Nursa")
    text = text.replace("100i", "one hundred eye")
    text = text.replace("125a", "one twenty five ay")
    text = text.replace("135c", "one thirty five see")
    text = text.replace("300i", "three hundred eye")
    text = text.replace("315p", "three fifteen pee")
    text = text.replace("325a", "three twenty five ay")
    text = text.replace("350r", "three fifty are")
    text = text.replace("400i", "four hundred eye")
    text = text.replace("600i", "six hundred eye")
    text = text.replace("890 Jump", "eight ninety jump")
    text = text.replace("890j", "eight ninety jump")
    text = text.replace("A1", "Ay one")
    text = text.replace("A2", "Ay two")
    text = text.replace("C1", "see one")
    text = text.replace("C2", "see two")
    text = text.replace("M2", "em two")
    text = text.replace("E1", "ee one")
    text = text.replace(" CL", "see el")
    text = text.replace("LN", "el en")
    text = text.replace(" ES", "ee es")
    text = text.replace(" MR", "em are")
    text = text.replace("VTOL", "vee tol")
    text = text.replace("C8", "see eight")
    text = text.replace("G12", "gee twelve")
    text = text.replace("G12a", "gee twelve ay")
    text = text.replace("G12r", "gee twelve are")
    text = text.replace("M50", "em fifty")
    text = text.replace("P52", "pee fifty two")
    text = text.replace("P72", "pee seventy two")
    text = text.replace("MPUV", "em puv")
    text = text.replace("DUR", "duration")
    text = text.replace("MIS", "miss")
    text = text.replace(" LX", "el ex")
    text = text.replace(" MX", "em ex")
    text = text.replace(" QI", "queue eye")
    text = text.replace("L-21", "el twenty one")
    text = text.replace("L-22", "el twenty two")
    text = text.replace("ROC", "rock")
    text = text.replace(" TAC", "tack")
    text = text.replace("SRV", "S R V")
    text = text.replace("IFCS", "I F C S")
    text = text.replace("San'tok.yāi", "san toke yai")
    text = text.replace("X1", "ex one")
    text = text.replace("MK II", "mark two")

    # --- Manufacturers ---
    text = text.replace("ARGO", "Are-go")
    text = text.replace("RSI", "R S I")  # Force letter pronunciation
    text = text.replace("MISC ", "misk")  # Commonly pronounced "misk"
    text = text.replace("Aegis", "Ae-gis")

    
    # --- ALIENS ---
    text = text.replace("Vanduul", "van-duel")
    text = text.replace("Xi'an", "shee-anne")
    text = text.replace("Gatac", "Ga-tahk")
    text = text.replace("Aopoa", "A-oh poh-ah")
    text = text.replace("Tevarin", "Tev-are-in")
    text = text.replace("Banu ", "Ban-new")
    text = text.replace("Wikelo", "wick-el-oh")
    text = text.replace("Khartu", "Kar-too")
    text = text.replace("Khartu-al", "Kar-too-all")

    # --- WEAPONS & EQUIPMENT ---
    text = text.replace("EMP", "E M P")
    text = text.replace("Stor-all", "Store all")
    text = text.replace("Omnisky", "om-nee-sky")
    text = text.replace("VK", "Vee kay")


    # --- GRAVITY ---
    # "4.5G" or "9Gs" -> "Gees"
    text = re.sub(r"\b(\d+)G\b", r"\1 Gees", text)
    text = re.sub(r"\b(\d+)Gs\b", r"\1 Gees", text)

    return text


# ==========================================
#             CORE AUDIO ENGINE
# ==========================================

def process_audio(text, voice_id=DEFAULT_VOICE_ID, ship_name=None, subfolder=None, tag="generic"):
    """
    0. CLEAN THE TEXT.
    1. Checks for cached files.
    2. Checks budget.
    3. Calls ElevenLabs API if needed.
    4. Saves file.
    """
    text = clean_text_for_speech(text)

    global session_chars_used
    
    voice_name = VOICE_MAP.get(voice_id, "unknown").lower()
    existing_files = get_cached_files(voice_id, ship_name, subfolder, tag)
    
    # REUSE LOGIC: 80% chance reuse, OR if we have > 5 files
    should_reuse = (len(existing_files) >= 5) or (len(existing_files) > 0 and random.random() < 0.8)

    if should_reuse:
        chosen_file = random.choice(existing_files)
        print(f"♻️ REUSING CACHE: {os.path.basename(chosen_file)}")
        return chosen_file, text

    # BUDGET CHECK
    if not can_afford_generation():
        if existing_files:
            print("⚠️ BUDGET LOW: Forcing cache reuse.")
            return random.choice(existing_files), text
        else:
            print("❌ BUDGET CRITICAL: No audio generated.")
            return None, "Communications Offline."

    # GENERATION LOGIC
    # Create unique filename: {tag}_{hash}_{voice}.mp3
    text_hash = hashlib.md5(text.encode()).hexdigest()[:6]
    clean_tag = re.sub(r'[^a-zA-Z0-9]', '_', tag.lower())[:15]
    filename = f"{clean_tag}_{text_hash}_{voice_name}.mp3"
    
    save_folder = get_storage_path(voice_id, ship_name, subfolder)
    full_path = os.path.join(save_folder, filename)
    
    print(f"🎙️ GENERATING NEW via ElevenLabs: '{text}'")
    
    try:
        # --- THE ELEVENLABS CONNECTION ---
        audio_stream = client_el.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2"
            # voice_settings=ElevenLabs.VoiceSettings(
            #     stability=0.5,       # Higher = clearer (0.3 - 0.8 range)
            #     similarity_boost=0.75
        )
        
        # Write the stream chunks to the file
        with open(full_path, "wb") as f:
            for chunk in audio_stream:
                f.write(chunk)
                
        session_chars_used += len(text)
        print(f"💾 SAVED to: {full_path}")
        print(f"💸 Cost: {len(text)} chars | Session Total: {session_chars_used}")
        return full_path, text
        
    except Exception as e:
        print(f"❌ ERROR connecting to ElevenLabs: {e}")
        return None, str(e)


# ==========================================
#          TACTICAL INTELLIGENCE
# ==========================================

def get_intro_advice(ship_name):
    """Cycles 1->2->3 based on fatigue count."""
    global session_counters
    count = session_counters.get(ship_name, 0)
    
    if count >= INTRO_FATIGUE_LIMIT:
        return None 
    
    data = load_json(SHIPS_DB_PATH).get(ship_name)
    if not data: return None

    token_index = (count % 3) + 1 
    advice_key = f"advice_{token_index}"
    session_counters[ship_name] = count + 1
    
    return data.get(advice_key, "Visual contact confirmed.")

def generate_tactical_insights(target_ship_name):
    """Compares User Ship vs Target Ship stats."""
    db = load_json(TACTICAL_DB_PATH)
    user_stats = db.get(USER_SHIP_NAME, {}).get("stats")
    target = db.get(target_ship_name)
    
    if not target: return []
    target_stats = target.get("stats", {})
    
    if not user_stats or not target_stats:
        return ["Insufficient telemetry for comparative analysis."]

    insights = []
    
    # Math Logic
    pitch_diff = target_stats['pitch'] - user_stats['pitch']
    if pitch_diff > 5:
        insights.append(f"Target has superior nose authority (+{pitch_diff} deg).")
    elif pitch_diff < -5:
        insights.append(f"You have the turn advantage. Stay close.")

    hp_diff = user_stats['hull_hp'] - target_stats['hull_hp']
    if hp_diff > 2000:
        insights.append(f"You have superior durability (+{hp_diff} HP).")
    elif hp_diff < -2000:
        insights.append("Target is heavily armored.")

    return insights

def get_next_deep_dive_tokens(target_ship_name, count=2):
    """Manages the queue of insights."""
    global insight_queues
    if target_ship_name not in insight_queues or not insight_queues[target_ship_name]:
        insight_queues[target_ship_name] = generate_tactical_insights(target_ship_name)
    
    queue = insight_queues[target_ship_name]
    tokens = queue[:count]
    insight_queues[target_ship_name] = queue[count:]
    return tokens

def interpret_intent(user_text):
    """Uses Gemini to figure out what the user wants."""
    prompt = (
        f"Analyze request: '{user_text}'. "
        "Return JSON with keys: 'intent' (tactical_intro, deep_dive, other) and 'ship' (ship name or null). "
        "Rules: "
        "1. If the user mentions a ship name (e.g. 'Gladius', 'Hammerhead'), set intent to 'tactical_intro'. "
        "2. If the user asks for 'more', 'compare', 'deep', or 'yes', set intent to 'deep_dive'. "
        "Example: 'Hammerhead' -> {'intent': 'tactical_intro', 'ship': 'hammerhead'}"
    )
    try:
        response = model.generate_content(prompt)
        clean_text = response.text.replace('```json', '').replace('```', '').strip()
        data = json.loads(clean_text)
        
        # DEBUG PRINT: See exactly what Gemini thinks
        print(f"🤔 AI THOUGHTS: {data}")
        
        # SAFETY NET: If we found a ship but Gemini got shy about the intent, force it.
        if data.get("ship") and data.get("intent") == "other":
             data["intent"] = "tactical_intro"
             
        return data
    except Exception as e: 
        print(f"⚠️ INTENT ERROR: {e}")
        return {"intent": "unknown", "ship": None}

# ==========================================
#              FLASK ENDPOINT
# ==========================================

@app.route('/query', methods=['POST'])
def query_endpoint():
    global active_target  # <--- Allow us to read/write this global
    
    user_input = request.json.get("text", "")
    print(f"👂 HEARD: {user_input}")
    
    # 1. Understand Intent
    data = interpret_intent(user_input)
    intent = data.get("intent")
    ship_name = data.get("ship")

    # --- STATE RECOVERY LOGIC ---
    # If we want a deep dive but didn't say the name, use the active target
    if intent == "deep_dive" and not ship_name:
        if active_target:
            print(f"🧠 CONTEXT RECOVERY: Assuming target is '{active_target}'")
            ship_name = active_target
        else:
            print("⚠️ CONTEXT FAIL: User asked for deep dive but no target is active.")
            
    # If we are starting a NEW intro, update the active target
    if intent == "tactical_intro" and ship_name:
        active_target = ship_name
    # ----------------------------

    response_text = ""
    subfolder = None
    target_tag = "generic"

    # 2. Logic Router
    if intent == "tactical_intro" and ship_name:
        target_tag = ship_name
        advice = get_intro_advice(ship_name)
        if advice:
            response_text = f"Contact confirmed. {ship_name.title()}. {advice}"
            if random.random() < 0.15:
                response_text += " Shall I compare specs?"
        else:
            response_text = f"Contact {ship_name.title()}. No new data."

    elif intent == "deep_dive" and ship_name:
        subfolder = "Analysis Response"
        target_tag = ship_name
        
        insights = get_next_deep_dive_tokens(ship_name, count=2)
        if insights:
            # Use Gemini to smooth out the math insights
            prompt = (
                f"You are Sarah, a tactical officer. Convert these data points into a brief warning: "
                f"{json.dumps(insights)}. "
                "CRITICAL FORMATTING FOR SPEECH: Do not use symbols like '+', '-', '%', or abbreviations like 'deg'. "
                "Write 'plus', 'minus', 'percent', and 'degrees'. "
                "Write numbers as you would say them (e.g., 'forty-eight hundred' instead of '4800')."
            )
            try:
                gemini_resp = model.generate_content(prompt)
                response_text = gemini_resp.text
            except:
                response_text = " ".join(insights)
        else:
            response_text = "Analysis complete. No further tactical advantages detected."
            
    else:
        # Fallback chatter
        response_text = "Command received. Systems nominal."

    # 3. Speak
    file_path, spoken_text = process_audio(
        response_text, 
        ship_name=ship_name, 
        subfolder=subfolder, 
        tag=target_tag
    )
    
    if file_path:
        return jsonify({"file": file_path, "text": spoken_text})
    else:
        return jsonify({"error": "Audio failed"}), 500

if __name__ == '__main__':
    # Ensure dirs exist
    if not os.path.exists(SHIPS_LIBRARY_PATH): os.makedirs(SHIPS_LIBRARY_PATH)
    if not os.path.exists(STANDARD_LIBRARY_PATH): os.makedirs(STANDARD_LIBRARY_PATH)

    print(f"-------------------------------------------")
    print(f"AI Co-Pilot Server Online (Voice: Sarah)")
    print(f"Flying: {USER_SHIP_NAME.title()}")
    check_account_health()
    print(f"-------------------------------------------")
    app.run(port=5000)