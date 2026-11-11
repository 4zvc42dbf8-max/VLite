"""VLite: pousse une notification Web Push sur l'iPhone quand de nouvelles vidéos arrivent.
Lit videos.json (écrit par collect.py), subs.json (abonnements enregistrés par l'app) et
notified.json (vidéos déjà annoncées). Clé privée VAPID : variable d'environnement VAPID_PRIVATE.
Sans dépendance autre que « cryptography »."""
import base64, json, os, sys, time, urllib.request, urllib.error
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:vlite@users.noreply.github.com")
MAX_AGE_H = 36          # on n'annonce pas une vidéo plus vieille que ça
MAX_PUSH = 3            # au-delà, un seul message groupé

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def unb64u(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def vapid_key(priv_b64):
    return ec.derive_private_key(int.from_bytes(unb64u(priv_b64), "big"), ec.SECP256R1())

def pub_b64(key):
    return b64u(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

def vapid_headers(endpoint, key, now=None):
    u = urlparse(endpoint)
    exp = int((now or time.time()) + 12 * 3600)
    h = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    c = b64u(json.dumps({"aud": u.scheme + "://" + u.netloc, "exp": exp, "sub": SUBJECT}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign((h + "." + c).encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {"Authorization": "vapid t=%s.%s.%s, k=%s" % (h, c, sig, pub_b64(key))}

def encrypt(payload, p256dh, auth, as_priv=None, salt=None):
    """RFC 8291 (aes128gcm)."""
    ua_pub = unb64u(p256dh); auth_s = unb64u(auth)
    as_priv = as_priv or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth_s, b"WebPush: info\0" + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\0").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\0").derive(ikm)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + ct

def send(sub, payload, key, ttl=6 * 3600):
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    hd = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "normal", "Content-Length": str(len(body))}
    hd.update(vapid_headers(sub["endpoint"], key))
    req = urllib.request.Request(sub["endpoint"], data=body, headers=hd, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print("  réseau :", e); return 0

def load(p, d):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return d

def age_h(pub):
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 0

def build(new):
    """Un message par vidéo (3 max), sinon un message groupé."""
    if len(new) <= MAX_PUSH:
        return [{"title": v.get("channel") or "VLite", "body": v.get("title", ""), "tag": "v-" + v["id"], "id": v["id"], "n": 1} for v in new]
    chans = []
    for v in new:
        c = v.get("channel")
        if c and c not in chans: chans.append(c)
    return [{"title": "%d nouvelles vidéos" % len(new), "body": ", ".join(chans[:4]) + ("…" if len(chans) > 4 else ""), "tag": "vlite-new", "n": len(new)}]

def main():
    vids = (load("videos.json", {}) or {}).get("videos", [])
    st = load("notified.json", None)
    first = st is None
    done = set((st or {}).get("ids", []))
    cur = [v for v in vids if v.get("id")]
    if first:                                   # première exécution : on ne notifie pas tout l'historique
        json.dump({"ids": sorted(v["id"] for v in cur)}, open("notified.json", "w")); print("Initialisation :", len(cur), "vidéos connues"); return
    new = [v for v in cur if v["id"] not in done and age_h(v.get("published")) <= MAX_AGE_H]
    new.sort(key=lambda v: str(v.get("published")))
    subs = load("subs.json", [])
    pk = os.environ.get("VAPID_PRIVATE", "").strip()
    if new and subs and pk:
        key = vapid_key(pk); alive = []
        for s in subs:
            ok = True
            for m in build(new):
                code = send(s, m, key)
                print(" ", s["endpoint"][:48], code)
                if code in (404, 410): ok = False; break
            if ok: alive.append(s)
        if len(alive) != len(subs): json.dump(alive, open("subs.json", "w"), indent=1)
    elif new:
        print("Nouvelles vidéos :", len(new), "- pas d'abonné ou clé VAPID_PRIVATE absente")
    json.dump({"ids": sorted(done | {v["id"] for v in cur})[-3000:]}, open("notified.json", "w"))

if __name__ == "__main__":
    main()
