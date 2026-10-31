"""Synthetic multilingual (English / Hinglish / Hindi) training + evaluation corpus for the intent model.

Honest scope: this is SYNTHETIC data authored for the prototype. The model, features and calibration pipeline are
real, but production accuracy must be re-measured on Paytm's own labelled tickets. Held-out evaluation uses
phrases and fragments the model never saw in training (template-level generalisation), plus typo noise.
"""
from __future__ import annotations

import random
from typing import Optional

INTENTS = ["payment_failed", "duplicate_debit", "settlement_held", "dispute", "emi_query", "claim_status", "soundbox", "unknown"]
LANGS = ["en", "hinglish", "hi"]

# (opener, issue, money) fragments for the compositional intent
PAYMENT = {
    "en": (["my payment", "the payment", "my transaction", "my UPI payment", "my order payment", "my recharge", "the money transfer"],
           ["failed", "did not go through", "got declined", "is stuck", "shows pending", "was unsuccessful", "is not complete"],
           ["but money was deducted", "and the amount was debited", "but my account was charged", "and I have not received a refund",
            "and the money has not come back", "yet the order is not confirmed"]),
    "hinglish": (["mera payment", "mera transaction", "UPI payment", "order ka payment", "recharge", "paise bhejna"],
                 ["fail ho gaya", "nahi hua", "atak gaya", "pending dikha raha hai", "complete nahi hua", "decline ho gaya"],
                 ["lekin paisa kat gaya", "par amount debit ho gaya", "aur refund nahi aaya", "paise wapas nahi aaye", "phir bhi order confirm nahi hua"]),
    "hi": (["मेरा पेमेंट", "मेरा ट्रांज़ैक्शन", "UPI पेमेंट", "ऑर्डर का भुगतान", "मेरा रिचार्ज"],
           ["फेल हो गया", "पूरा नहीं हुआ", "अटक गया", "पेंडिंग दिख रहा है", "डिक्लाइन हो गया"],
           ["लेकिन पैसे कट गए", "पर राशि डेबिट हो गई", "और रिफंड नहीं आया", "पैसे वापस नहीं आए", "फिर भी ऑर्डर कन्फर्म नहीं हुआ"]),
}

PHRASES: dict[str, dict[str, list[str]]] = {
    "duplicate_debit": {
        "en": ["I was charged twice", "money got debited two times", "double deduction for one order", "same payment deducted again", "duplicate charge on my account",
               "amount cut twice for the same purchase", "it charged me two times for one booking", "I see two debits for a single order"],
        "hinglish": ["mere paise do baar kat gaye", "ek hi order ke liye double deduction hua", "same payment dobara kat gaya", "do baar charge ho gaya",
                     "ek hi cheez ke liye paisa do baar kata", "account se do debit dikh rahe hain ek order par", "dobara paisa kat gaya same order ka"],
        "hi": ["मेरे पैसे दो बार कट गए", "एक ही ऑर्डर पर दोहरी कटौती हुई", "दोबारा पैसे कट गए", "एक ही खरीद के लिए दो बार चार्ज हुआ",
               "खाते से एक ही ऑर्डर के दो डेबिट दिख रहे हैं", "वही भुगतान दोबारा कट गया"],
    },
    "settlement_held": {
        "en": ["my settlement is on hold", "settlement not received in my bank", "payout stuck", "money from sales has not reached my bank account",
               "settlement delayed since yesterday", "why is my settlement held", "my daily payout did not arrive", "the settlement amount is not credited to my account"],
        "hinglish": ["mera settlement ruka hua hai", "settlement bank mein nahi aaya", "payout atak gaya hai", "sale ka paisa bank mein nahi aaya",
                     "settlement hold pe hai", "kal se settlement nahi aaya", "mera payout abhi tak credit nahi hua", "settlement kyun roka gaya hai"],
        "hi": ["मेरा सेटलमेंट रुका हुआ है", "सेटलमेंट बैंक में नहीं आया", "पेआउट अटका हुआ है", "बिक्री का पैसा खाते में नहीं आया",
               "कल से सेटलमेंट नहीं आया", "सेटलमेंट क्यों रोका गया है", "मेरा पेआउट अभी तक जमा नहीं हुआ"],
    },
    "dispute": {
        "en": ["a chargeback was raised against me", "customer filed a dispute on this order", "I got a dispute notice", "how do I respond to this chargeback",
               "customer claims goods not received and opened a dispute", "bank reversed a payment as chargeback", "need to fight a chargeback before the deadline"],
        "hinglish": ["mere khilaf chargeback aaya hai", "customer ne dispute daala hai", "dispute notice mila hai", "chargeback ka jawab kaise dun",
                     "customer kehta hai saman nahi mila aur dispute khola", "bank ne payment chargeback mein ulta diya", "deadline se pehle chargeback ka reply dena hai"],
        "hi": ["मेरे खिलाफ चार्जबैक आया है", "ग्राहक ने विवाद दर्ज किया है", "विवाद का नोटिस मिला है", "चार्जबैक का जवाब कैसे दूँ",
               "ग्राहक कहता है सामान नहीं मिला और विवाद खोला", "समय सीमा से पहले चार्जबैक का जवाब देना है"],
    },
    "emi_query": {
        "en": ["when is my EMI due", "what is my loan installment date", "I cannot pay this month's EMI", "need to postpone my EMI", "EMI amount and due date please",
               "money is tight can the EMI be moved", "how much loan is still outstanding", "can I change my installment date"],
        "hinglish": ["meri EMI kab due hai", "loan ki kist ki date batao", "is mahine EMI bharna mushkil hai", "EMI aage kar sakte ho kya", "kist kitni baaki hai",
                     "paise kam hain EMI thodi der se de sakta hoon", "loan ka outstanding kitna hai", "kist ki date badal sakte hain kya"],
        "hi": ["मेरी ईएमआई कब देय है", "लोन की किस्त की तारीख बताइए", "इस महीने किस्त भरना मुश्किल है", "ईएमआई आगे बढ़ा सकते हैं क्या",
               "किस्त कितनी बाकी है", "पैसे कम हैं ईएमआई थोड़ी देर से दे सकता हूँ", "किस्त की तारीख बदल सकते हैं क्या"],
    },
    "claim_status": {
        "en": ["status of my insurance claim", "what is happening with my claim", "claim documents pending?", "when will my insurance claim be settled", "claim update please",
               "did you receive my hospital bills for the claim", "my policy claim has not moved"],
        "hinglish": ["mere insurance claim ka status batao", "claim ka kya hua", "claim kab milega", "claim ke documents pending hain kya", "mera claim aage nahi badha",
                     "hospital ke bill claim ke liye mile ya nahi", "policy claim ka update do"],
        "hi": ["मेरे बीमा क्लेम का स्टेटस बताइए", "क्लेम का क्या हुआ", "क्लेम कब मिलेगा", "क्लेम के दस्तावेज़ पेंडिंग हैं क्या", "मेरा क्लेम आगे नहीं बढ़ा",
               "अस्पताल के बिल क्लेम के लिए मिले या नहीं"],
    },
    "soundbox": {
        "en": ["my soundbox is not working", "soundbox does not speak payment alerts", "the speaker device is offline", "soundbox has no sound",
               "soundbox not connecting to network", "the payment speaker at my shop is silent", "my soundbox keeps going offline"],
        "hinglish": ["mera soundbox awaaz nahi kar raha", "soundbox offline hai", "payment aane par speaker nahi bolta", "soundbox network se connect nahi ho raha",
                     "dukaan ka speaker chup hai", "soundbox baar baar offline ho jata hai", "soundbox mein koi awaaz nahi aati"],
        "hi": ["मेरा साउंडबॉक्स काम नहीं कर रहा", "साउंडबॉक्स ऑफ़लाइन है", "स्पीकर में आवाज़ नहीं आ रही", "भुगतान आने पर साउंडबॉक्स नहीं बोलता",
               "दुकान का स्पीकर चुप है", "साउंडबॉक्स बार बार ऑफ़लाइन हो जाता है"],
    },
    "unknown": {
        "en": ["help me", "something is wrong with my account", "can I talk to someone", "the QR code looks strange", "I have a question about my app",
               "how do I change my phone number", "why is my app slow", "urgent please call me", "the shop name on the QR is different", "is this payment link safe",
               "my app keeps crashing", "I cannot log in to the app", "how do I update my address", "where can I see my transaction history", "how to add a new bank account",
               "I want to close my account", "how do I reset my UPI PIN", "what are your customer care hours"],
        "hinglish": ["mujhe help chahiye", "mere account mein kuch gadbad hai", "kisi se baat karni hai", "QR code ajeeb lag raha hai", "app slow chal raha hai",
                     "phone number kaise badlun", "QR par dukaan ka naam alag hai", "ye payment link safe hai kya",
                     "app baar baar crash ho raha hai", "login nahi ho paa raha", "address kaise update karun", "transaction history kahan dikhegi", "naya bank account kaise jodun",
                     "account band karna hai", "UPI PIN kaise reset karun", "customer care ka time kya hai"],
        "hi": ["मुझे मदद चाहिए", "मेरे खाते में कुछ गड़बड़ है", "किसी से बात करनी है", "QR कोड अजीब लग रहा है", "ऐप धीमा चल रहा है",
               "फोन नंबर कैसे बदलूँ", "QR पर दुकान का नाम अलग है", "क्या यह पेमेंट लिंक सुरक्षित है",
               "ऐप बार बार क्रैश हो रहा है", "लॉगिन नहीं हो पा रहा", "पता कैसे अपडेट करूँ", "ट्रांज़ैक्शन हिस्ट्री कहाँ दिखेगी", "नया बैंक खाता कैसे जोड़ूँ",
               "खाता बंद करना है", "UPI पिन कैसे रीसेट करूँ", "कस्टमर केयर का समय क्या है"],
    },
}
PREFIX = ["", "", "", "hello", "hi team", "namaste", "bhai", "please", "sir", "urgent:", "नमस्ते", "सुनिए", "hello sir", "ji"]
SUFFIX = ["", "", "", "thanks", "dhanyavaad", "please help", "धन्यवाद", "jaldi dekho", "kya karu", "please check", "मदद करें", "asap"]


def _typo(text: str, rng: random.Random, p: float) -> str:
    out = []
    for w in text.split(" "):
        if len(w) > 4 and rng.random() < p:
            k = rng.randrange(1, len(w) - 2)
            mode = rng.random()
            if mode < 0.5:
                w = w[:k] + w[k + 1] + w[k] + w[k + 2:]  # transpose
            elif mode < 0.8:
                w = w[:k] + w[k + 1:]  # drop
            else:
                w = w[:k] + w[k] + w[k:]  # double
        out.append(w)
    return " ".join(out)


ASR_SUBS = {"paisa": "pesa", "paise": "pese", "kat": "cut", "gaya": "gya", "fail": "fel", "settlement": "settelment", "refund": "refand", "payment": "paymant",
            "nahi": "nahin", "hai": "he", "aaya": "aya", "ruka": "rukka", "soundbox": "sound box", "chargeback": "charge back", "kist": "kisht", "claim": "clame"}
DEV_SUBS = [("ब", "व"), ("श", "स"), ("ट", "त"), ("ड", "द"), ("ं", ""), ("ँ", "")]


def asr_noise(text: str, rng: random.Random, p: float = 0.3) -> str:
    """Imitates speech-recognition errors: phonetic respellings, dropped words, merged/split words, dropped matras."""
    out = []
    for w in text.split(" "):
        lw = w.lower()
        if lw in ASR_SUBS and rng.random() < p * 2:
            w = ASR_SUBS[lw]
        elif any(0x0900 <= ord(c) <= 0x097F for c in w) and rng.random() < p:
            a, b = rng.choice(DEV_SUBS)
            w = w.replace(a, b, 1)
        elif len(w) > 3 and rng.random() < p * 0.3:
            continue  # dropped word
        out.append(w)
    t = " ".join(out)
    if rng.random() < p * 0.5 and " " in t:
        i = t.index(" "); t = t[:i] + t[i + 1:]  # merged words
    return t


def _decorate(text: str, rng: random.Random, noise: float) -> str:
    if rng.random() < 0.25:
        text += " " + rng.choice(["T-%04d" % rng.randint(1000, 9999), "Rs %d" % rng.choice([199, 799, 1250, 4999, 12000]), "₹%s" % rng.choice(["1,249", "6,800", "32,000"])])
    t = f"{rng.choice(PREFIX)} {text} {rng.choice(SUFFIX)}".strip()
    t = " ".join(t.split())
    if noise:
        t = _typo(t, rng, noise)
    if rng.random() < 0.2:
        t = t.lower()
    if rng.random() < 0.15:
        t = t.rstrip(".") + rng.choice(["!", "!!", "?", "..."])
    return t


def _part(i: int) -> int:
    """0 = train, 1 = validation (fits temperature), 2 = test (never touched)."""
    return {3: 1, 4: 2}.get(i % 5, 0)


def _payment_phrases(lang: str, part: int) -> list[str]:
    op, iss, mon = PAYMENT[lang]
    out = []
    for o in op:
        for j, s in enumerate(iss):
            for k, m in enumerate(mon):
                if max(_part(j), _part(k)) == part:
                    out.append(f"{o} {s} {m}")
    return out


def phrases(intent: str, lang: str, part: int) -> list[str]:
    if intent == "payment_failed":
        return _payment_phrases(lang, part)
    return [p for i, p in enumerate(PHRASES[intent][lang]) if _part(i) == part]


def generate(n_per_intent: int, seed: int, part: int = 0, noise: float = 0.0, intents: Optional[list[str]] = None, asr: float = 0.0) -> list[tuple[str, str, str]]:
    """Returns (text, intent, lang). part 0=train, 1=validation, 2=test: each uses phrases/fragments the others never saw."""
    rng = random.Random(seed)
    rows: list[tuple[str, str, str]] = []
    for intent in intents or INTENTS:
        for i in range(n_per_intent):
            lang = LANGS[i % len(LANGS)]
            pool = phrases(intent, lang, part)
            base = rng.choice(pool)
            if rng.random() < 0.12 and intent != "unknown":  # code-mix: tail from another language
                other = rng.choice([l for l in LANGS if l != lang])
                base = base + " " + rng.choice(phrases(intent, other, part))[:40]
            txt = _decorate(base, rng, noise)
            if asr:
                txt = asr_noise(txt, rng, asr)
            rows.append((txt, intent, lang))
    rng.shuffle(rows)
    return rows
