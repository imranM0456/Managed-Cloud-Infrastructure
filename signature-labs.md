# Labs: Hash → Key Pair → Signature

Goal: understand "GitHub signs a token, Entra verifies it" from the bottom up.
Time: ~30 min. Run everything in one empty folder.

```bash
mkdir sig-lab && cd sig-lab
```

---

## Lab 1: Hash (fingerprint)

**Idea:** a hash turns any input into a fixed-size fingerprint. Same input gives the same output. Any change gives a completely different output. You can't go backwards.

```bash
echo -n "hello"  | sha256sum
echo -n "hello." | sha256sum
echo -n "a much longer message, still the same output length" | sha256sum
```

**What is happening**
- `echo -n` prints the text with no trailing newline. A hash covers exact bytes, so an extra newline would change the result.
- `sha256sum` hashes those bytes with SHA-256 and prints 64 hex characters (= 32 bytes).
- Line 2 differs from line 1 by one character, and the output is totally different.
- Line 3 is much longer, and the output is still 64 hex characters.

**Check yourself**
- Is the output size related to the input size? (No, always 32 bytes.)
- Can you recover "hello" from the hash? (No, one-way.)

---

## Lab 2: Make a key pair

**Idea:** two linked keys. Private = secret, signs. Public = shareable, verifies.

**Basics**

keys

Pick two huge secret primes: p and q.

n = p × q. This is the modulus, and it goes in the public key (with e = 65537).
The private key (d) is computed from p and q. Without knowing p and q, you can’t compute d.
The attacker sees n and e. To get d they must factor n into p and q. That is the impossible step.
Toy version from earlier: n = 33 is public. Anyone can see 33 = 3 × 11 and derive d = 7. With a 2048-bit n, nobody can.

What “2048” means

It’s the size of n in bits: n is a number written with 2048 binary digits.
That’s about 617 decimal digits.
p and q are each about 1024 bits, so p × q is about 2048 bits.
2048 bits = 256 bytes. That’s why msg.sig is 256 bytes.

```bash
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -pubout -out public.pem
head -1 private.pem public.pem
```

**What is happening**
- `genrsa -out private.pem 2048` generates a 2048-bit RSA key pair and writes it to `private.pem`. This file holds the secret part.
- `rsa -in private.pem -pubout -out public.pem` copies only the public half into `public.pem`. Nothing new is generated.
- `head -1` shows the first line of each file: `BEGIN PRIVATE KEY` and `BEGIN PUBLIC KEY`. These are labels, so tools know what the file holds.

**Check yourself**
- Which file can you safely share? (`public.pem`)
- Which one must never leave your machine? (`private.pem`)

---

## Lab 3: Sign and verify

**Idea:** sign = hash the message, then transform the hash with the private key. Verify = hash the message again, undo the signature with the public key, compare.

```bash
echo -n "hello" > msg.txt

# sign with the PRIVATE key
openssl dgst -sha256 -sign private.pem -out msg.sig msg.txt

# verify with the PUBLIC key
openssl dgst -sha256 -verify public.pem -signature msg.sig msg.txt
```

**Expected:** `Verified OK`

**What is happening**
- `-sha256` hashes `msg.txt` into a fingerprint.
- `-sign private.pem` transforms that fingerprint with the private key. The result is the signature, saved as binary in `msg.sig` (256 bytes, same as the key size).
- `-verify public.pem -signature msg.sig` hashes `msg.txt` itself, undoes the signature with the public key, and compares the two fingerprints. They match, so it prints `Verified OK`.
- This is exactly what Entra does with a GitHub token.

```bash
ls -l msg.sig        # 256 bytes
xxd msg.sig | head -3   # raw binary, unreadable
```

---

## Lab 4: Break it on purpose

### 4a. Tamper with the message

```bash
echo -n "hello." > msg.txt
openssl dgst -sha256 -verify public.pem -signature msg.sig msg.txt
```

**Expected:** `Verification failure`

**Why:** the new message has a different hash. The signature still unwraps to the *old* hash, so the two don't match. To sign the new text you would need the private key.

### 4b. Wrong public key (an attacker's own pair)

```bash
echo -n "hello" > msg.txt                      # restore original
openssl genrsa -out other.pem 2048
openssl rsa -in other.pem -pubout -out other_pub.pem
openssl dgst -sha256 -verify other_pub.pem -signature msg.sig msg.txt
```

**Expected:** `Verification failure`

**Why:** only the matching public key can undo the signature. A different key gives garbage. This is why Entra only trusts the keys of the issuer named in your federated credential.

### 4c. Public key can't sign

```bash
openssl dgst -sha256 -sign public.pem -out x.sig msg.txt
```

**Expected:** an error.

**Why:** signing needs the private part, and `public.pem` doesn't contain it.

---

## Lab 5: Same idea inside a JWT (optional, after 1-4 click)

```bash
pip install pyjwt cryptography
```

```python
import jwt, time

priv = open("private.pem").read()
pub  = open("public.pem").read()

claims = {"iss": "https://my-fake-github",
          "sub": "repo:me/lab:environment:prod",
          "aud": "api://AzureADTokenExchange",
          "exp": int(time.time()) + 300}

token = jwt.encode(claims, priv, algorithm="RS256")
print(token)    # header.payload.signature

print(jwt.decode(token, pub, algorithms=["RS256"],
                 audience="api://AzureADTokenExchange",
                 issuer="https://my-fake-github"))
```

**What is happening**
- `RS256` = RSA signature over SHA-256, the same operation as Lab 3.
- `jwt.encode` builds `header.payload`, signs that string with the private key, and appends the signature.
- `jwt.decode` verifies the signature with the public key, then checks `exp`, `aud`, and `iss`.

---

## Cleanup

```bash
shred -u private.pem other.pem 2>/dev/null || rm -f private.pem other.pem
```

Lab keys are throwaway, but delete them anyway. Never paste real private keys or tokens into chats or online tools.

---

## Write these 5 lines in your own words

1. A hash is ...
2. The private key ... and the public key ...
3. Signing means ...
4. Verification fails when ...
5. A valid signature proves ...

(Answers: fixed-size fingerprint of any input; private signs, public verifies; hash the message then transform the hash with the private key; the message changed, the key is wrong, or the signature is fake; the message came from the private key holder and wasn't changed.)
