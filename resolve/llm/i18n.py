"""Language detection and grounded message templates (English, Hindi, Hinglish).

Templates are the *grounding* for every outbound message: the LLM may rephrase tone, but numbers, ids and
commitments come only from system-of-record facts passed in `facts`. Other Indic languages go through Sarvam
translation when a key is configured (see sarvam.py); otherwise the message falls back to English.
"""
from __future__ import annotations

import re
from typing import Any

HINGLISH_MARKERS = {
    "paisa", "paise", "kat", "gaya", "gayi", "hua", "hai", "nahi", "nhi", "mera", "meri", "mere", "kya", "kyun", "kab",
    "aapka", "aap", "mujhe", "bhai", "rukha", "ruka", "wapas", "dobara", "kist", "bhejo", "karo", "kar", "do", "abhi",
    "ho", "raha", "rahi", "tha", "thi", "se", "ka", "ki", "ko", "mein", "me", "haan", "han", "theek", "dikkat", "problem",
    "atka", "atak", "aaya", "aayi", "kab", "tak", "hoga", "chahiye", "lagta", "bataya", "diya", "liya",
}
DEVANAGARI = re.compile(r"[\u0900-\u097F]")


SCRIPTS = [("bn", 0x0980, 0x09FF), ("pa", 0x0A00, 0x0A7F), ("gu", 0x0A80, 0x0AFF), ("od", 0x0B00, 0x0B7F), ("ta", 0x0B80, 0x0BFF),
           ("te", 0x0C00, 0x0C7F), ("kn", 0x0C80, 0x0CFF), ("ml", 0x0D00, 0x0D7F), ("ur", 0x0600, 0x06FF)]
NATIVE = ("en", "hi", "hinglish")


def detect_language(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    for code, lo, hi in SCRIPTS:
        if sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters) > 0.3:
            return code
    dev = sum(1 for c in letters if DEVANAGARI.match(c))
    if dev / len(letters) > 0.3:
        return "hi"
    words = re.findall(r"[a-zA-Z']+", text.lower())
    hits = sum(1 for w in words if w in HINGLISH_MARKERS)
    if hits >= 2 and hits / max(1, len(words)) >= 0.15:
        return "hinglish"
    return "en"


def inr(x: float) -> str:
    return "₹{:,.0f}".format(x)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


T: dict[str, dict[str, str]] = {
    "ack": {
        "en": "Hi {name}, I'm on it. I'm checking your account now.",
        "hi": "नमस्ते {name}, आपके खाते की जाँच चल रही है।",
        "hinglish": "Namaste {name}, aapke account ki jaanch chal rahi hai.",
    },
    "payment_reversal_pending": {
        "en": "Your payment of {amount} ({txn}) did not go through, but the money was debited. A reversal is already in progress and is expected within {eta_h} hours. Nothing more is needed from you.",
        "hi": "आपका {amount} का भुगतान ({txn}) पूरा नहीं हुआ, लेकिन राशि कट गई थी। रिवर्सल शुरू हो चुका है और {eta_h} घंटे के भीतर पैसे वापस आ जाएँगे। आपको कुछ करने की ज़रूरत नहीं है।",
        "hinglish": "Aapka {amount} ka payment ({txn}) complete nahi hua, lekin paisa kat gaya tha. Reversal shuru ho chuka hai aur {eta_h} ghante ke andar paise wapas aa jayenge. Aapko kuch karne ki zaroorat nahi hai.",
    },
    "payment_refund_initiated": {
        "en": "Your payment of {amount} ({txn}) failed after the amount was debited. We have started a refund to your original payment source (ref {refund_id}).",
        "hi": "आपका {amount} का भुगतान ({txn}) विफल रहा जबकि राशि कट गई थी। हमने आपके मूल भुगतान स्रोत में रिफंड शुरू कर दिया है (रेफ़ {refund_id})।",
        "hinglish": "Aapka {amount} ka payment ({txn}) fail ho gaya jabki paisa kat gaya tha. Humne aapke original payment source mein refund shuru kar diya hai (ref {refund_id}).",
    },
    "payment_success_info": {
        "en": "Good news: your payment {txn} of {amount} actually went through and the merchant has been credited.",
        "hi": "अच्छी खबर: आपका {amount} का भुगतान {txn} सफल रहा और व्यापारी को राशि मिल चुकी है।",
        "hinglish": "Achhi khabar: aapka {amount} ka payment {txn} successful raha aur merchant ko paisa mil chuka hai.",
    },
    "no_debit_info": {
        "en": "Your payment {txn} did not complete and no money was debited from your account.",
        "hi": "आपका भुगतान {txn} पूरा नहीं हुआ और आपके खाते से कोई राशि नहीं कटी है।",
        "hinglish": "Aapka payment {txn} complete nahi hua aur aapke account se koi paisa nahi kata hai.",
    },
    "duplicate_refund": {
        "en": "You were charged twice for the same order. The duplicate charge of {amount} ({txn}) is being refunded to your original payment source (ref {refund_id}).",
        "hi": "एक ही ऑर्डर के लिए आपसे दो बार पैसे कटे थे। {amount} की दोहरी कटौती ({txn}) आपके मूल भुगतान स्रोत में लौटाई जा रही है (रेफ़ {refund_id})।",
        "hinglish": "Ek hi order ke liye aapse do baar paisa kata tha. {amount} ki duplicate deduction ({txn}) aapke original payment source mein wapas ki ja rahi hai (ref {refund_id}).",
    },
    "settlement_released": {
        "en": "Your settlement {sid} of {amount} was on hold because the bank account name did not match your KYC. We re-verified your account, it passed, and the hold has been released.",
        "hi": "आपका {amount} का सेटलमेंट {sid} रुका हुआ था क्योंकि बैंक खाते का नाम आपकी केवाईसी से मेल नहीं खा रहा था। हमने आपके खाते की दोबारा जाँच की, वह सफल रही और होल्ड हटा दिया गया है।",
        "hinglish": "Aapka {amount} ka settlement {sid} ruka hua tha kyunki bank account ka naam aapki KYC se match nahi kar raha tha. Humne account dobara verify kiya, wo pass ho gaya aur hold hata diya gaya hai.",
    },
    "settlement_need_docs": {
        "en": "Your settlement {sid} of {amount} is on hold because your KYC has expired. Please upload a fresh KYC document in the app. The request has been sent, and the hold will be reviewed as soon as it arrives.",
        "hi": "आपका {amount} का सेटलमेंट {sid} रुका हुआ है क्योंकि आपकी केवाईसी की अवधि समाप्त हो गई है। कृपया ऐप में नया केवाईसी दस्तावेज़ अपलोड करें। अनुरोध भेज दिया गया है, दस्तावेज़ मिलते ही होल्ड की समीक्षा होगी।",
        "hinglish": "Aapka {amount} ka settlement {sid} ruka hua hai kyunki aapki KYC expire ho gayi hai. Kripya app mein nayi KYC document upload karein. Request bhej di gayi hai, document milte hi hold review hoga.",
    },
    "settlement_pending_normal": {
        "en": "Your settlement {sid} of {amount} is being processed normally and will be paid out in the next cycle.",
        "hi": "आपका {amount} का सेटलमेंट {sid} सामान्य रूप से प्रोसेस हो रहा है और अगले चक्र में भुगतान हो जाएगा।",
        "hinglish": "Aapka {amount} ka settlement {sid} normal tareeke se process ho raha hai aur agle cycle mein payout ho jayega.",
    },
    "dispute_pending_approval": {
        "en": "I've prepared the evidence pack for dispute {did} ({amount}, due {deadline}). A specialist is approving the filing now.",
        "hi": "मैंने विवाद {did} ({amount}, अंतिम तिथि {deadline}) के लिए साक्ष्य पैक तैयार कर दिया है। विशेषज्ञ अभी फाइलिंग को मंज़ूरी दे रहे हैं।",
        "hinglish": "Maine dispute {did} ({amount}, deadline {deadline}) ke liye evidence pack taiyaar kar diya hai. Specialist abhi filing approve kar rahe hain.",
    },
    "dispute_filed": {
        "en": "Dispute {did} has been answered with your evidence pack, before the deadline.",
        "hi": "विवाद {did} का जवाब आपके साक्ष्य पैक के साथ अंतिम तिथि से पहले दर्ज कर दिया गया है।",
        "hinglish": "Dispute {did} ka jawab aapke evidence pack ke saath deadline se pehle file kar diya gaya hai.",
    },
    "dispute_need_evidence": {
        "en": "For dispute {did} we are missing: {missing}. Please share them before {deadline} so we can respond in time.",
        "hi": "विवाद {did} के लिए हमारे पास ये नहीं हैं: {missing}। कृपया {deadline} से पहले साझा करें ताकि समय पर जवाब दिया जा सके।",
        "hinglish": "Dispute {did} ke liye humare paas ye nahi hain: {missing}. Kripya {deadline} se pehle share karein taaki time par jawab diya ja sake.",
    },
    "emi_offer": {
        "en": "Your EMI of {emi} is due on {due}. If it is tight this month, this EMI can be moved once to {new_due}. Would you like that? Reply YES to confirm.",
        "hi": "आपकी {emi} की ईएमआई {due} को देय है। अगर इस महीने दिक्कत है तो इसे एक बार {new_due} तक आगे किया जा सकता है। क्या आप ऐसा चाहते हैं? पुष्टि के लिए 'हाँ' लिखें।",
        "hinglish": "Aapki {emi} ki EMI {due} ko due hai. Agar is mahine dikkat hai to ise ek baar {new_due} tak aage kiya ja sakta hai. Kya aap chahte hain? Confirm karne ke liye 'haan' likhein.",
    },
    "emi_rescheduled": {
        "en": "Done. Your EMI is now due on {new_due}.",
        "hi": "हो गया। आपकी ईएमआई अब {new_due} को देय है।",
        "hinglish": "Ho gaya. Aapki EMI ab {new_due} ko due hai.",
    },
    "emi_info": {
        "en": "Your EMI of {emi} is due on {due}. Outstanding balance: {outstanding}.",
        "hi": "आपकी {emi} की ईएमआई {due} को देय है। बकाया राशि: {outstanding}।",
        "hinglish": "Aapki {emi} ki EMI {due} ko due hai. Outstanding balance: {outstanding}.",
    },
    "emi_declined": {
        "en": "No problem, I've left your EMI as it is. It is due on {due}.",
        "hi": "कोई बात नहीं, आपकी ईएमआई जैसी थी वैसी ही रखी गई है। देय तिथि: {due}।",
        "hinglish": "Koi baat nahi, aapki EMI jaisi thi waisi hi rakhi hai. Due date: {due}.",
    },
    "claim_docs_needed": {
        "en": "Your claim {cid} is waiting on: {docs}. Please upload them in the app. The request has been sent.",
        "hi": "आपका क्लेम {cid} इन दस्तावेज़ों का इंतज़ार कर रहा है: {docs}। कृपया ऐप में अपलोड करें। अनुरोध भेज दिया गया है।",
        "hinglish": "Aapka claim {cid} in documents ka wait kar raha hai: {docs}. Kripya app mein upload karein. Request bhej di gayi hai.",
    },
    "claim_status": {
        "en": "Your claim {cid} is {status}. Expected update in about {eta} days.",
        "hi": "आपका क्लेम {cid} {status} है। लगभग {eta} दिनों में अपडेट मिलेगा।",
        "hinglish": "Aapka claim {cid} {status} hai. Lagbhag {eta} din mein update milega.",
    },
    "soundbox_rebooted": {
        "en": "Your Soundbox {did} was offline. It has been restarted remotely and is back online.",
        "hi": "आपका साउंडबॉक्स {did} ऑफ़लाइन था। उसे दूर से रीस्टार्ट कर दिया गया है और वह फिर से चालू है।",
        "hinglish": "Aapka Soundbox {did} offline tha. Use remotely restart kar diya gaya hai aur wo wapas online hai.",
    },
    "soundbox_replacement": {
        "en": "Your Soundbox {did} could not be restored remotely. A replacement has been dispatched under warranty.",
        "hi": "आपका साउंडबॉक्स {did} दूर से ठीक नहीं हो सका। वारंटी के तहत नया डिवाइस भेज दिया गया है।",
        "hinglish": "Aapka Soundbox {did} remotely theek nahi ho saka. Warranty ke under naya device bhej diya gaya hai.",
    },
    "escalated": {
        "en": "I've passed your case to a specialist with every detail, so you won't need to repeat anything. Reference {ticket}.",
        "hi": "मैंने आपका मामला पूरी जानकारी के साथ विशेषज्ञ को सौंप दिया है, आपको कुछ दोहराना नहीं पड़ेगा। रेफ़रेंस {ticket}।",
        "hinglish": "Maine aapka case poori jaankari ke saath specialist ko de diya hai, aapko kuch dohrana nahi padega. Reference {ticket}.",
    },
    "approval_wait": {
        "en": "A specialist is verifying the action I prepared. You will get an update here as soon as it is confirmed.",
        "hi": "मेरे द्वारा तैयार की गई कार्रवाई की विशेषज्ञ जाँच कर रहे हैं। पुष्टि होते ही आपको यहीं अपडेट मिलेगा।",
        "hinglish": "Maine jo action taiyaar kiya hai use specialist verify kar rahe hain. Confirm hote hi aapko yahin update milega.",
    },
    "scam_warning": {
        "en": "This looks like a possible scam. Please never share your OTP, PIN or screen with anyone, even someone who says they are from Paytm. I've flagged this to our fraud team (ref {ticket}). If money was lost, also report it at 1930 or cybercrime.gov.in.",
        "hi": "यह एक संभावित धोखाधड़ी लग रही है। कृपया अपना ओटीपी, पिन या स्क्रीन किसी के साथ साझा न करें, चाहे वह खुद को पेटीएम का बताए। इसे फ्रॉड टीम को भेज दिया गया है (रेफ़ {ticket})। अगर पैसे कटे हैं तो 1930 या cybercrime.gov.in पर भी रिपोर्ट करें।",
        "hinglish": "Ye ek possible scam lag raha hai. Kripya apna OTP, PIN ya screen kisi ke saath share na karein, chahe wo khud ko Paytm ka bataye. Ise fraud team ko bhej diya gaya hai (ref {ticket}). Agar paise kate hain to 1930 ya cybercrime.gov.in par bhi report karein.",
    },
    "injection_note": {
        "en": "For your security, I can only act on verified account records, so I ignored the part of your message that asked for something else.",
        "hi": "आपकी सुरक्षा के लिए कार्रवाई केवल सत्यापित खाता रिकॉर्ड के आधार पर होती है, इसलिए संदेश के दूसरे अनुरोध को नज़रअंदाज़ किया गया है।",
        "hinglish": "Aapki suraksha ke liye action sirf verified account records ke basis par hota hai, isliye message ke doosre request ko ignore kiya gaya hai.",
    },
    "access_denied": {
        "en": "I can only discuss transactions on your own account. I have not shared anything about that transaction, and this request has been logged.",
        "hi": "मैं केवल आपके अपने खाते के लेन-देन पर बात कर सकता हूँ। उस लेन-देन की कोई जानकारी साझा नहीं की गई है और यह अनुरोध दर्ज कर लिया गया है।",
        "hinglish": "Main sirf aapke apne account ke transactions par baat kar sakta hoon. Us transaction ki koi jaankari share nahi ki gayi hai aur ye request log kar li gayi hai.",
    },
    "novel_escalation": {
        "en": "Thanks for flagging this. It needs a specialist's eyes, and I've shared everything I found so far (ref {ticket}).",
        "hi": "बताने के लिए धन्यवाद। इसे विशेषज्ञ को देखना होगा, और अब तक की सारी जानकारी उन्हें दे दी गई है (रेफ़ {ticket})।",
        "hinglish": "Batane ke liye dhanyavaad. Ise specialist ko dekhna hoga, aur ab tak ki saari jaankari unhe de di gayi hai (ref {ticket}).",
    },
    "action_failed_escalation": {
        "en": "We hit a technical problem while processing this. A specialist has your case with full details (ref {ticket}), and nothing has been done twice.",
        "hi": "इसे प्रोसेस करते समय तकनीकी समस्या आई। विशेषज्ञ के पास आपका पूरा मामला है (रेफ़ {ticket}) और कोई भी कार्रवाई दोबारा नहीं हुई है।",
        "hinglish": "Ise process karte waqt technical problem aayi. Specialist ke paas aapka poora case hai (ref {ticket}) aur koi bhi action do baar nahi hua hai.",
    },
    "proactive_outage": {
        "en": "{bank} is facing temporary issues. Your payment of {amount} ({txn}) may show as failed or pending. If money was debited it will be reversed to your source, and you do not need to do anything.",
        "hi": "{bank} में अस्थायी समस्या चल रही है। आपका {amount} का भुगतान ({txn}) विफल या लंबित दिख सकता है। अगर राशि कटी है तो वह आपके स्रोत में लौटा दी जाएगी, आपको कुछ करने की ज़रूरत नहीं है।",
        "hinglish": "{bank} mein temporary dikkat chal rahi hai. Aapka {amount} ka payment ({txn}) failed ya pending dikh sakta hai. Agar paisa kata hai to wo aapke source mein wapas aa jayega, aapko kuch karne ki zaroorat nahi hai.",
    },
    "proactive_settlement": {
        "en": "We noticed your settlement {sid} of {amount} was on hold for a name mismatch. We fixed it before you had to ask, and it has been released.",
        "hi": "हमने देखा कि आपका {amount} का सेटलमेंट {sid} नाम न मिलने के कारण रुका था। आपके पूछने से पहले ही उसे ठीक कर दिया गया और जारी कर दिया गया है।",
        "hinglish": "Humne dekha ki aapka {amount} ka settlement {sid} naam match na hone se ruka tha. Aapke poochne se pehle hi use theek karke release kar diya gaya hai.",
    },
}


def render(kind: str, lang: str, **facts: Any) -> str:
    tpl = T.get(kind, {})
    text = tpl.get(lang) or tpl.get("en") or kind
    return text.format_map(_Safe(facts))


YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "haan", "han", "ha", "haa", "haanji", "kar do", "kardo", "theek hai", "thik hai", "हाँ", "हां", "जी", "ठीक है", "कर दो"}
NO = {"no", "n", "nope", "nahi", "nahin", "mat karo", "नहीं", "ना", "मत करो"}


def parse_yes_no(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"[^\w\s\u0900-\u097F]", " ", t)
    words = set(t.split())
    if any(p in t for p in ("kar do", "theek hai", "thik hai", "कर दो", "ठीक है")) or words & YES:
        return "yes"
    if any(p in t for p in ("mat karo", "मत करो")) or words & NO:
        return "no"
    return "unclear"
