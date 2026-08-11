# GenAI Usage & Code Refinement Log

Required appendix per `Project1_SocketProgramming_2026.pdf`, Section 2.4 item 6 / Section 4.3.
Tool used: **Claude Code** (Anthropic), interactive CLI agent with direct filesystem/shell access to
this repository. All prompts below are transcribed verbatim from the session; AI output is
reproduced unedited, followed by the student's critical analysis and what was actually kept,
changed, or rejected.

---

## Entry 1

**Prompt used:**
> I only code the basic level how to test

**Raw GenAI output (summary of actions taken):**
The AI read `README.md`, `common.py`, `server.py`, `client.py` to understand the protocol, then
started `server.py` in the background and drove `client.py` itself via piped stdin
(`printf 'user alice\npass password123\nput /tmp/hello.txt\nget hello.txt\nquit\n' | python3 client.py 127.0.0.1`)
to confirm login, upload, and download worked end-to-end on `127.0.0.1`. It also ran `diff` between
the original and downloaded file to prove byte-for-byte correctness, and tested two failure paths
(wrong password → `530`, `RETR` on a missing file → `550`). It then proposed a manual two-terminal
test procedure (start server, start client, run `user`/`pass`/`put`/`get`/`quit`) and suggested
testing with a file larger than 1024 bytes to exercise multi-packet UDP reassembly.

**Refinement & problem solving:**
- The AI's own local loopback test is *not* a substitute for the required live demo — it only
  proves the protocol logic is self-consistent on one machine. The group still performed the real
  test across two machines (local client → Azure VM server) documented in Entry 4–7 below.
- The suggestion to test with a file >1024 bytes (to exercise the multi-datagram code path in
  `handle_stor`/`handle_retr`) was accepted and later used as a real test case, since the Basic
  Level rubric requires correct upload/download, and a single-packet test alone would not catch a
  reassembly bug (e.g. an off-by-one in `sorted(chunks)`).

---

## Entry 2

**Prompt used:**
> ftp> put '/Users/doancongpho/Documents/Hcmus Education/Junior/Term 3/Internetworking protocol/Socket/hybrid_ftp/server_storage/hello.txt'
> [!] Local file not found: '/Users/doancongpho/Documents/Hcmus

**Raw GenAI output:**
> The REPL's `put`/`get` parsing just does a plain whitespace split — it doesn't understand
> shell-style quotes. Your path has spaces in it (`Term 3`, `Internetworking protocol`), so
> `'/Users/.../put`...`hello.txt'` gets cut at the first space, and the leading `'` gets included
> literally in the filename it looks for.
>
> Simplest fix: avoid typing a full path with spaces... Or copy it to a space-free path first...
>
> If you want `put`/`get` to actually support quoted/spaced paths, the fix is in `client.py`'s
> `repl()` — swap `arg.split(maxsplit=1)` for `shlex.split(arg)`, which understands quotes. Want
> me to make that change?

**Refinement & problem solving:**
- Root cause analysis (naive `str.split()` instead of shell-aware tokenizing) is correct and was
  verified by inspecting `client.py`'s `repl()` function directly — `parts = line.split(maxsplit=1)`
  confirms the claim.
- The `shlex.split` fix was **not applied** — this is out of scope for the Basic Level rubric and
  was worked around instead by not putting test files under this repo's space-containing path
  (`/tmp/hello.txt` was used for later tests, see Entry 1). Decision: avoid unnecessary scope
  creep in the codebase; the assignment does not require CLI argument quoting.

---

## Entry 3

**Prompt used:**
> [pasted terminal output showing `python3 server` → "No such file or directory", then
> `python3 server.py` → `OSError: [Errno 48] Address already in use`, then `ps`]

**Raw GenAI output:**
The AI ran `lsof -nP -iUDP:2122 -iTCP:2121`, identified a leftover Python process (PID 47066 —
the AI's own earlier background test server from Entry 1 that was never cleaned up) holding both
ports, killed it with `kill 47066`, and re-verified the ports were free.

**Refinement & problem solving:**
- Confirms an operational lesson, not a code bug: `python3 server` (missing `.py`) fails because
  there is no such file; `Address already in use` means a previous server process is still bound
  to the port. Fix procedure recorded for the group: `lsof -nP -iTCP:2121 -iUDP:2122` → `kill <PID>`.
- No source code was changed for this entry.

---

## Entry 4

**Prompt used:**
> [pasted `socket.gaierror: [Errno 8] nodename nor servname provided, or not known` after running
> `python3 client.py http://20.249.210.239/`]
> tôi muốn kết nối tới vm của tôi

**Raw GenAI output:**
> Lỗi là do bạn truyền cả URL (`http://20.249.210.239/`) làm host, nhưng `client.py` chỉ nhận địa
> chỉ IP hoặc hostname thuần, không phải URL — nên `socket.connect()` không resolve được.
>
> Sửa lại, chỉ truyền IP: `python3 client.py 20.249.210.239`
>
> Trước khi chạy, cần đảm bảo: server đang chạy trên VM; firewall/NSG của Azure VM mở port 2121
> (TCP) và 2122 (UDP); địa chỉ là public IP của VM.

**Refinement & problem solving:**
- Verified correct: `FTPClient.__init__` calls `socket.connect((host, control_port))` with `host`
  used as a raw hostname/IP — passing a full URL string is not a valid hostname and DNS resolution
  correctly fails with `gaierror`.
- The group used this to confirm the actual test target: an Azure VM running `server.py`, reached
  from a local machine as the client, which is the realistic deployment scenario for the live
  demo requirement (Section 4.5 item 2).

---

## Entry 5

**Prompt used:**
> ok

**Raw GenAI output:**
The AI ran `nc -zv -w 5 20.249.210.239 2121` (succeeded) and `nc -zvu -w 3 20.249.210.239 2122`
(succeeded), and explicitly caveated that a UDP `nc` success only means no immediate ICMP
"port unreachable" was returned — it does not prove the server actually receives/processes the
datagram, since UDP has no handshake.

**Refinement & problem solving:**
- Accepted the caveat: the real correctness test was the subsequent live `put`/`get` session
  against the VM (Entry 6), not the `nc` probe alone.

---

## Entry 6 — the crash bug

**Prompt used:**
> [pasted a full Python traceback: `ConnectionError: Server closed the connection.` raised from
> `client.py` line 49 (`_read_reply`), triggered from `put()` → `command("STOR ...")`]

**Raw GenAI output:**
> Lỗi `ConnectionError: Server closed the connection.` xảy ra ngay khi đọc reply đầu tiên cho lệnh
> `STOR` — nghĩa là server đã tự đóng kết nối/crash trước khi kịp gửi `150...`
>
> Nguyên nhân nhiều khả năng nhất: `parse_packet()` gọi
> `struct.unpack(HEADER_FMT, raw[:9])` mà không kiểm tra độ dài gói tin. VM có IP public nên UDP
> port 2122 dễ nhận gói rác/scan từ internet. Nếu gói đó ngắn hơn 9 byte, `struct.error` sẽ bay ra
> — và exception này không được bắt ở đâu cả (chỉ có `except socket.timeout`), nên nó crash luôn
> `handle_client`, bay tiếp lên `main()` (chỉ bắt `KeyboardInterrupt`) → crash toàn bộ server
> process.

**Refinement & problem solving:**
- This diagnosis was a hypothesis at the time it was given — the AI had not yet seen the VM's own
  server-side traceback (it does not have SSH access to the VM). The student was asked to check
  the VM terminal to confirm. The hypothesis was later treated as confirmed by elimination: no
  other code path between "230 Login successful" being sent and the next `command()` call touches
  the TCP connection at all except the unguarded `struct.unpack` in the HELLO-wait block.
- Follow-up questions from the student (Entries 7–8, below) probed alternative explanations
  (simple restart, or an Azure NSG firewall change) before accepting this root cause — this
  back-and-forth is the "critical analysis" the rubric asks for, not blind acceptance of the first
  AI explanation.

---

## Entry 7

**Prompt used:**
> giờ lại không lỗi nảy bị gì crash z ?

**Raw GenAI output:**
> Có thể là do bạn ... đã khởi động lại `server.py` trên VM — server cũ bị crash (chết hẳn), bạn
> chạy lại thì nó tạo process mới... Bản chất lỗi cũ (server bị crash) vẫn còn đó trong code, chỉ
> là chưa bị trigger lại lần này — vì phụ thuộc vào việc có gói UDP "rác" nào bay tới port 2122
> đúng lúc `PASS` đang chờ `HELLO` hay không, nên nó không lúc nào cũng xảy ra.

**Refinement & problem solving:**
- Correctly frames the bug as a **race condition** dependent on external network traffic timing,
  not a deterministic bug — important distinction for the oral defense, since "it works now" does
  not mean the bug is fixed.

---

## Entry 8

**Prompt used:**
> oke và thiệt ra có một thay đổi là tôi config trong server 2121 và 2122 cho udp tcp trước đó k
> config có thể lỗi từ đó

**Raw GenAI output:**
> Config firewall/NSG chắc chắn là bước cần thiết ... nhưng khả năng cao không phải nguyên nhân
> khiến lần sau không crash, vì lúc chạy `nc -zv` test cả hai port (trước khi bạn gặp lỗi crash ở
> lần `put` đó), cả hai đều đã pass — nghĩa là tại thời điểm xảy ra crash, port đã mở và thông rồi.
> ... Cái thật sự khác giữa 2 lần là: server process bị crash chết, và bạn khởi động lại nó.

**Refinement & problem solving:**
- The AI used its own earlier tool-call transcript (the `nc` results in Entry 5, timestamped
  before the crash in Entry 6) as evidence to rule out the student's alternative hypothesis. This
  is a case where the *student* proposed a theory and the AI pushed back with a timeline argument
  rather than just agreeing — recorded here specifically because the rubric singles out "critical
  analysis identifying errors ... in the AI output" but the same scrutiny was applied
  bidirectionally in this session.

---

## Entry 9 — the fix (raw code output)

**Prompt used:**
> đầu tiên vá lỗi đi

**Raw GenAI output (unedited diffs applied to the repo):**

`common.py`, `parse_packet()`:
```python
def parse_packet(raw):
    """Returns (pkt_type, seq, payload, checksum_valid).

    Packets shorter than the header (e.g. stray/non-protocol UDP traffic
    hitting the data port) are reported as invalid instead of raising, so a
    malformed datagram can't take down the whole server.
    """
    if len(raw) < HEADER_SIZE:
        return None, None, b"", False
    pkt_type, seq, checksum = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
    payload = raw[HEADER_SIZE:]
    valid = (zlib.crc32(payload) & 0xFFFFFFFF) == checksum
    return pkt_type, seq, payload, valid
```

`server.py`, PASS handler (HELLO wait):
```python
                    try:
                        raw, caddr = udp_sock.recvfrom(1024)
                        pkt_type, _, _, valid = parse_packet(raw)
                        if valid and pkt_type == PKT_HELLO:
                            session.client_data_addr = caddr
                    except socket.timeout:
                        pass
```

`server.py`, `main()` accept loop:
```python
    try:
        while True:
            conn, addr = tcp_sock.accept()
            try:
                handle_client(conn, addr, udp_sock)   # single-threaded: one client at a time
            except Exception as e:
                # A single bad session (malformed input, unexpected client
                # behavior, etc.) must not take the whole server down.
                print(f"[!] Session with {addr} crashed: {e!r}")
    except KeyboardInterrupt:
```

**Refinement & problem solving (student's critical analysis):**
1. **Verified the fix, didn't just trust it.** Started a local server, sent a deliberately
   malformed 2-byte UDP datagram to port 2122 (simulating stray internet noise) at the exact
   moment the server would be waiting for `HELLO`, then immediately ran a full `put`/`get` session.
   Result: `put` succeeded (`226 Transfer complete.`), and `lsof` confirmed the server process was
   still alive afterward — before the fix, this exact sequence killed the process outright.
2. **Caught and understood a side-effect the AI didn't fully call out up front:** `get` returned
   `425 Can't open data connection` in the same test, because the malformed packet "consumed" the
   single `recvfrom()` slot the server uses to learn the client's UDP address — the real `HELLO`
   packet was never seen. This is **not** a new bug introduced by the fix; it is the pre-existing
   Basic Level limitation of a single fixed data-channel handshake (documented in the project's
   own `README.md` under "Deliberately not implemented"). Confirmed this is acceptable Basic Level
   behavior rather than asking the AI to also fix it, to avoid unscoped changes.
3. **Rejected scope creep the AI could have added but didn't:** a full reliable-UDP layer
   (ACK/retransmit) would prevent this class of issue more thoroughly, but that is explicitly an
   Excellent Level rubric item, not something to bolt on ad hoc while fixing a Basic Level crash.
4. **Remaining manual step (not done by the AI):** the patched `common.py`/`server.py` still need
   to be copied to the Azure VM and the server process there restarted — the AI only has access to
   the local filesystem, not the VM.

---

## Entry 10 — Advanced Level implementation

**Prompt used:**
> I want my project can display all of the levels like in the future if there is some alternative
> technique use we should create option for user to choose or they can config and don't completly
> change the feature. Now code for me advanced feature. We've coded the basic so far.

**Raw GenAI output (summary of actions taken):**
Before writing any code, the AI flagged that `README.md`'s "Deliberately not implemented" section had
merged the real Advanced and Excellent rubric tiers into one undifferentiated list, and asked two
clarifying questions: (1) which specific items actually count as "Advanced Level," and (2) how the
user/config choice should be exposed (CLI flags, a config file, or protocol commands only). The student
answered by pointing to the actual assignment PDF (`Project1_SocketProgramming_2026.pdf`, one directory
above the repo, not previously read by the AI) and chose "config file."

The AI read the PDF directly and extracted the real Section 1.3 rubric: Advanced Level = (a) binary file
handling (`TYPE I`), (b) directory tree navigation (`CWD`/`CDUP`/`MKD`/`RMD`/`LIST`/`NLST`/`STAT`), (c)
Active/Passive mode switching (`PORT`/`PASV`), (d) a multi-threaded/multi-process concurrent server. It
then implemented all four, gated behind a new `config.py`/`config.ini` layer whose defaults reproduce
Basic Level exactly (deleting `config.ini`, or any key in it, changes nothing). Specific changes:
- `common.py`: added `format_port_arg`/`parse_port_arg`/`parse_pasv_reply` (RFC 959 address encoding)
  and new `Reply` entries (`pasv`, `port_ok`, `cwd_ok`, `dir_created`, `status`); updated `TYPE`/`MODE`
  reply text since the old wording (e.g. "reserved for Advanced Level active/passive mode") was now
  stale.
- `server.py`: `Session` gained `cwd`, `data_sock`/`owns_data_sock` (per-session UDP socket for
  ACTIVE/PASSIVE vs. the old shared fixed-port socket), and `resolve_data_endpoint()` was changed to
  return `(socket, address)` instead of just an address. Added `handle_pasv`/`handle_port` (each opens
  a dedicated ephemeral UDP socket), `resolve_path`/`safe_path` (confines all file/dir arguments under
  `STORAGE_ROOT`, rejecting `..` escapes), `handle_cwd`/`handle_mkd`/`handle_rmd`/`handle_list`/
  `handle_stat`/`handle_mdtm`, and a `threading.Thread`-per-connection accept loop gated by
  `config.ini`'s `[server] threading` key, plus a lock-protected `_active_sessions` table printed to
  the server log on every connect/disconnect/mode-change.
- `client.py`: added `set_active()`/`set_passive()`/`set_fixed()` and matching REPL commands
  (`active`/`passive`/`fixed`/`cwd`/`cdup`/`mkd`/`rmd`/`ls`/`nlst`/`stat`/`mdtm`/`type`), plus a shared
  `_recv_data_payload()` helper reused by both `get()` and the new `list_dir()`.

The AI then ran its own verification (all on `localhost`, single machine):
1. **Basic Level regression** — default `config.ini` (`threading=single`, `data_mode=fixed`), a
   `put`/`get` round-trip, `diff`'d byte-for-byte against the source file.
2. **Directory tree** — `mkd sub` → `cwd sub` → `put` a file into it → `ls`/`nlst`/`stat`/`mdtm`, then
   `cdup` and `ls` again to confirm the file only appears inside `sub/`.
3. **Binary transfer in both Advanced modes** — a 5000-byte random binary file, `put`+`get` round-tripped
   once over PASSIVE mode and once over ACTIVE mode, both `diff`'d byte-for-byte.
4. **Concurrency** — `threading=thread`, two separate client processes (`alice`, `bob`) each doing a
   20KB-random-file `put`+`get` round trip in PASSIVE mode *simultaneously* (backgrounded shell jobs),
   both `diff`'d byte-for-byte afterward; the server log's session table was inspected and showed both
   `#4` and `#5` listed concurrently with `mode=PASSIVE` before either disconnected.

It then updated `README.md` and `CLAUDE.md` to document the config keys, the three data-channel modes,
and one explicitly disclosed deviation from RFC 959: in real active-mode FTP the server doesn't need to
report its own port back to the client, because TCP's server-initiated `connect()` reuses one
bidirectional socket; this project's UDP data channel is connectionless, so the upload direction (STOR
in ACTIVE mode) needs an explicit destination port, which is why `Reply.port_ok()` appends
`"; server data port N"` to the `200` reply instead of a bare `200 Command OK.`.

**Refinement & problem solving:**
- The AI asking which rubric items count as "Advanced Level" *before* writing code — rather than
  guessing from the group's own earlier (and, it turned out, imprecise) README — avoided building
  against a stale self-authored summary instead of the actual PDF. Pointing the AI at the real spec
  file was the single most consequential correction in this entry: it changed the AI's assumed rubric
  from an unstructured Basic+Advanced+Excellent grab-bag into the four precise Advanced Level bullets
  the assignment actually grades.
- **[Student to fill in]**: explain in your own words *why* FIXED-mode sessions are not safely isolated
  under concurrency (they share one server-wide UDP socket, demuxed only by best-effort source-address
  filtering in `handle_stor`) while ACTIVE/PASSIVE sessions are (each gets its own per-session ephemeral
  socket via `handle_port`/`handle_pasv`) — this is exactly the "Active/Passive mode nuance" the rubric's
  Theoretical Understanding "Good/Very Good" tier asks for, and it's a genuine, disclosed limitation of
  this codebase, not a hypothetical.
- The AI's own tests only prove the protocol logic is self-consistent on one machine (same caveat as
  Entry 1) — they are not a substitute for a live two-machine demo exercising PASV/PORT and concurrent
  sessions, which Section 4.5 of the spec requires before the oral defense, including showing the active
  session table live in the server log.
- **[Student to fill in]**: any additional manual verification, pushback, or questions raised about the
  AI's design choices beyond what's captured above — in particular, whether the `Reply.port_ok()`
  deviation from RFC 959 was independently understood/re-derived, since Section 4.2's zero-tolerance
  policy means being unable to explain *why* that deviation exists on the spot is an automatic 0 for
  both Theoretical Understanding and Live Coding.

---

## Entry 11

**Prompt used:**
> những hàm trong common này hỗ trợ gì
> [pasted `format_port_arg`, `parse_port_arg`, `parse_pasv_reply` from `common.py`, added in Entry 10]

**Raw GenAI output:**
The AI explained, in Vietnamese matching the question's language, that these three functions implement
RFC 959's `h1,h2,h3,h4,p1,p2` address encoding shared by the `PORT` command and the `PASV` reply (a port
number doesn't fit in one 0–255 field, so it's split into two bytes `p1`/`p2` where
`port = p1*256 + p2`): `format_port_arg(ip, port)` encodes an `(ip, port)` tuple into that
comma-separated string, used client-side (`client.py`'s `set_active()`) when sending `PORT`;
`parse_port_arg(arg)` decodes it back into `(ip, port)`, used server-side (`server.py`'s `handle_port()`)
when receiving `PORT`, returning `None` on malformed input instead of raising; `parse_pasv_reply(text)`
regex-extracts the parenthesized substring from a full `227 Entering Passive Mode (...).` reply line and
hands it to `parse_port_arg`, used client-side (`set_passive()`) when receiving a `PASV` reply.

**Refinement & problem solving:**
- **[Student to fill in]**: confirm you can reproduce this explanation unaided and hand-trace one
  example end to end (e.g. port `54321` → `divmod(54321, 256)` = `(212, 49)` →
  `"h1,h2,h3,h4,212,49"` → decoded back as `212*256 + 49 = 54321`) — per Section 4.2, being unable to
  walk through this on the spot during the oral viva is an automatic 0 for Theoretical Understanding and
  Live Coding, regardless of whether the code itself works.

---

## Entry 12 — Excellent Level implementation (Go-Back-N + integrity verification)

**Prompt used:**
> good everything work correctly now move to excellent level
> '/Users/doancongpho/Documents/Hcmus Education/Junior/Term 3/Internetworking protocol/Socket/Project1_SocketProgramming_2026.pdf'

**Raw GenAI output (summary of actions taken):**
Before writing code, the AI flagged that the Excellent Level rubric (Section 1.3 of the spec) names three
required items — a custom reliable-UDP layer (RDT: ACKs, sequence numbers, timeout/retransmit, explicitly
offering Stop-and-Wait, Go-Back-N, or Selective Repeat as valid choices), sliding-window congestion/flow
control, and end-to-end MD5/SHA-256 integrity verification — and asked which RDT algorithm to build,
since the choice materially changes the packet format and both `server.py` and `client.py`. It recommended
Go-Back-N specifically because a single mechanism (one retransmit timer for the oldest unacked packet,
whole-window retransmission on timeout) satisfies both the RDT bullet and the separate sliding-window
bullet at once, versus Stop-and-Wait (no inherent window) or Selective Repeat (more state: per-packet ACK
tracking, receiver-side out-of-order buffering). The student chose Go-Back-N.

The AI then implemented, across all four files:
- `common.py`: a new `PKT_ACK` packet type (reusing the existing 9-byte header, no wire-format change);
  `gbn_send()`/`gbn_receive()` as the *single* shared Go-Back-N sender/receiver implementation (sliding
  window, cumulative ACK, one timer for the oldest unacked packet that retransmits the whole in-flight
  window on timeout, `PKT_FIN` folded into the same reliable stream as packet number N rather than sent
  unacknowledged); `compute_hash()` shared by both HASH's server and client sides.
- `config.py`/`config.ini`: new `[reliability]` (`mode = none|gbn`, `window_size`, `rto_ms`, `max_retries`)
  and `[integrity]` (`verify = true|false`, `algorithm = sha256|md5`) sections, both defaulting to exactly
  the old best-effort/no-verification behavior — appended to the student's live-deployment `config.ini`
  (which already had a real Azure VM IP and a passive port range set) without disturbing existing values.
- `server.py`: `_send_over_data_channel()`/`handle_stor()` branch on `RELIABILITY_MODE`; new `handle_hash()`
  and a `HASH` dispatcher branch (removed from `NOT_IMPLEMENTED`).
- `client.py`: `put()`/`_recv_data_payload()` branch the same way; new `hash_remote()`/`_verify_hash()` and
  a `hash` REPL command; `[integrity] verify` auto-runs verification after every `put`/`get`.

**Refinement & problem solving:**
- **Did not just trust the algorithm "looked right" — built an adversarial test.** Before touching
  `server.py`/`client.py` at all, the AI wrote a throwaway test harness
  (`/private/tmp/.../scratchpad/test_gbn.py`, not committed) that wraps a UDP socket's `sendto()` to
  randomly drop packets, and ran `gbn_send()`/`gbn_receive()` against it at 10–50% *simultaneous
  bidirectional* loss (both DATA and ACK packets dropped independently). One trial at 50% loss with the
  default `max_retries=50` initially reported as a naive "FAIL" — investigated and confirmed this was
  *correct* behavior (the sender's retry budget was legitimately exhausted while the receiver had, by
  chance, already fully reassembled the file — a case where the sender's own ACK confirmation was lost,
  not a data-integrity bug). The test's success criterion was corrected to the property that actually
  matters for an RDT layer: `gbn_send()` must never report success on corrupted/incomplete data (zero
  false positives), which held across every trial, including 10 repeated runs at 15% loss and an extreme
  50%-loss run given a larger retry budget to confirm it can still succeed given enough patience.
- **Full integration re-tested after the isolated algorithm passed**, not assumed to follow from it: a
  real `server.py`/`client.py` session over loopback with `mode=gbn` and `verify=true` (put/get/hash all
  matching, byte-`diff`-verified), `LIST`/`NLST` under GBN (they reuse the same send helper), a deliberate
  hash-mismatch check (querying `HASH` against a different file to confirm the digest actually changes,
  not just always reporting MATCH), and a full regression pass confirming `mode=none` (the untouched
  default) still behaves byte-identically to before this session.
- **Config changes were tested in isolation, then the student's real `config.ini` was restored exactly** —
  backed up before flipping `mode`/`verify`/`advertise_ip` for local testing (the live VM's `advertise_ip`
  would have broken loopback testing otherwise), restored after, and the diff was inspected
  (`git diff config.ini`) to confirm only the new `[reliability]`/`[integrity]` sections were added with
  nothing else touched.
- **Deliberately rejected scope creep**: Selective Repeat and adaptive/AIMD-style congestion control were
  both explicitly named and explicitly not implemented, with the reasoning (more receiver-side state;
  spec's "sliding window *or equivalent*" wording already satisfied by a fixed configurable window)
  recorded directly in the code comments and README rather than silently left out.
- `[student to fill in]`: independently re-derive why Go-Back-N retransmits the *whole* window on a single
  lost packet while Selective Repeat would not — this is the single most likely oral-viva question given
  the rubric's explicit "Flawless mastery of RDT states (Stop-and-Wait, GBN, SR)" line at the Excellent
  tier.

---

## Entry 13 — Post-implementation code walkthrough (Q&A)

**Prompt used (paraphrased sequence of follow-up questions, same session):**
> NOT_IMPLEMENTED = {...} những thứ đã build là đủ cho assignment chưa hay còn thiếu gì k
> [selection: `f.write(data)` / stored-file logging line] ... đã bỏ đi thứ tự của chunk rồi đúng k vậy thì
> nếu không dùng gbn thì sẽ không ghép lại data được à?
> [selection: `packets.append(make_packet(PKT_FIN, seq))`] dòng này là sao
> [selection: `def recv_line(conn):`] này làm gì
> [selection: `self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)`] conn ở client là control
> channel ak
> control channel qua ba level thay đổi ra sao

**Raw GenAI output (summary):**
A run of pure code-comprehension questions, no code changes. The AI: (1) audited the full spec against
what's built and reported the gap honestly — `STOU`/`APPE`/`DELE`/`RNFR`/`RNTO`/`ABOR`/`MODE B`/`MODE C`
are unimplemented but not named by any Section 1.3 rubric bullet, flagged that the PDF's own "Level"
column for the command table wasn't legible in what was extracted (so this is an inference, not a
confirmed fact), and separately flagged a real risk: the spec's literal wording "fully isolates client
sessions" is not met by FIXED-mode concurrency (a disclosed, existing limitation), so demos should use
Active/Passive mode. It also pointed out that most of the *assignment* (the 7-section technical report)
was still missing, not just code. (2) Confirmed the non-GBN `handle_stor` path still reorders chunks via
`sorted(chunks)` exactly as before — nothing was lost in the refactor, only consolidated so both branches
share one `f.write(data)` call. (3) Explained that `PKT_FIN`'s `seq` number is deliberately set to one past
the last DATA chunk so it rides in the *same* `packets` array as GBN's sliding window/ACK/retransmit
logic, making "transfer complete" itself reliable instead of a fire-and-forget UDP send. (4) Explained
`recv_line()`'s byte-at-a-time TCP read loop and its `\r`/`\n` handling. (5) Confirmed `self.conn` is the
TCP control socket (vs. `self.udp_sock` for data) by pointing at `SOCK_STREAM` and the `control_port`
argument. (6) Summarized how the control-channel command set grew across Basic → Advanced → Excellent,
with the key observation that Excellent Level's RDT layer adds *no* new control-channel commands or
replies at all (only `HASH` is new) — GBN vs. best-effort is decided entirely out-of-band via
`config.ini`, invisible on the control-channel wire, by deliberate design (documented in Entry 12).

**Refinement & problem solving:**
- These were verification questions, not blind acceptance — each answer was checked against the actual
  current line numbers/content of the file open in the IDE at the time (`server.py`, `common.py`,
  `client.py`) rather than answered from memory of what was written earlier in the session, catching that
  line numbers had shifted since Entry 12's edits.
- `[student to fill in]`: this back-and-forth is exactly the kind of "explain your own socket calls"
  competency the Theoretical Understanding rubric grades — record here whether these explanations were
  independently verified/reproduced, not just read and accepted.

---

## Entry 14 — Technical report, diagrams, and demo evidence analysis

**Prompt used:**
> Assignment không chỉ chấm code. Section 2.4 yêu cầu Technical Report gồm 7 phần, hiện chỉ có phần 6
> (GenAI log) là đang được duy trì: [list of 7 sections, items 1–5 and 7 marked missing] ... do it in
> folder docs create code mermaid for sequence diagram and write latex for technical report
> '.../testing_evidence' i have testing evident where file of rudp is complete and udp is missing

**Raw GenAI output (summary of actions taken):**
The AI inspected `testing_evidence/` (two PNGs the student had already produced from a real `put`/`get`
demo: `heavy_pic_rudp.png`, `heavy_pic_udp.png`) rather than taking the student's description at face
value, and derived concrete facts from the bytes themselves: both files share the same declared
2816×1536 dimensions and a valid PNG `IEND` trailer, but the `udp` file is exactly 1,203,200 bytes shorter;
the two files are byte-identical up to offset 3,399,680, and both that offset and the missing byte count
are exact multiples of `CHUNK_SIZE` (1024) — i.e. 1175 whole UDP datagrams were lost, not partial-packet
corruption. Confirmed pdflatex/mermaid-cli were available locally, then produced:
- `docs/diagrams.md` — 6 Mermaid diagrams (2 sequence diagrams: full TCP+UDP lifecycle including a
  simulated GBN retransmission, and Active/Passive negotiation; 4 flowcharts: server thread-dispatch, GBN
  sender state machine, GBN receiver state machine, client mode selection).
- `docs/render_diagrams.sh` — extracts each mermaid block and renders it to PNG via
  `npx @mermaid-js/mermaid-cli`, so the diagrams can be regenerated after any future code change instead
  of going stale.
- `docs/technical_report.tex` — a full LaTeX report covering all 7 mandatory sections: Sections 1–3
  (scenario/diagrams, data structures, flowcharts) filled in from the actual codebase; Section 7 (demo
  evidence) built around the byte-level RUDP-vs-UDP analysis above, with both evidence images embedded
  (recompressed from ~17MB combined to ~250KB); Sections 4–5 (Task Assignment Matrix, Self-Assessment &
  Peer Evaluation) left as clearly marked `% TODO(team)` placeholder tables, since those require real
  team/individual input the AI does not have; Section 6 explicitly notes that this Excellent Level GenAI
  log entry (Entry 12 above) was pending at time of writing.

**Refinement & problem solving:**
- **Did not claim the diagrams or report compiled without checking.** Rendered all 6 mermaid diagrams and
  hit one real syntax error (a semicolon inside a sequence-diagram message body — `"200 PORT command
  successful; server data port N."` — being misparsed as a mermaid statement separator); fixed and
  re-rendered. Compiled the LaTeX twice with `pdflatex -halt-on-error`, hit a fatal `\square` undefined
  (missing `amssymb` package) and two "float too large for page" warnings on the taller diagrams, fixed
  both (added the package; added `height=...,keepaspectratio` to every `\includegraphics`), and only
  reported success once a clean recompile produced zero warnings.
- **Rendered specific PDF pages back to images with Ghostscript to visually verify layout** (title/TOC
  page, the sequence-diagram page, the demo-evidence page with the embedded photos) rather than trusting
  "pdflatex exited 0" as sufficient — this caught nothing further wrong, but was how the earlier float and
  package errors would have been caught if they'd been purely cosmetic instead of fatal/warned.
- **Explicitly refused to fabricate what it didn't have**: the Task Assignment Matrix and Self-Assessment
  sections were left as visibly incomplete placeholder tables rather than invented plausible-looking
  content, since Section 4.1 of the spec ties individual grades to this matrix and fabricating it would be
  actively misleading, not just unhelpful.
- `[student to fill in]`: verify the byte-offset/chunk-count analysis in Section 7.1 by re-deriving it
  independently (e.g. `cmp` or a short Python diff of the two evidence PNGs) before presenting it as your
  own finding in the oral defense — it was computed once, by the AI, and not independently cross-checked
  by a second method.

---

## Entry 15 — Three robustness fixes: server-side upload integrity, listen() backlog, PASSIVE keepalive

**Prompt used:**
> I want to change it to server check (if the integrity check == fail drop file that user upload) drop
> the previous fix. `tcp_sock.listen(5 if THREADING_MODE == "thread" else 1)` this line say that only
> access 5 users is there any better option ?. and In the passive mode, the data channel ussually drop at
> NAT client. I want to have the mechanism to send small packet to keep this connection only drop if user
> exceed the idle time.

(This followed the AI's own prior write-up — reproduced in Entry 14's context, not repeated here — which
had flagged the client-side hash-verification flow's cross-platform `os.linesep` edge case as a documented
limitation rather than a fix. The student rejected leaving it as a documented limitation and asked for an
actual server-side mechanism instead.)

**Raw GenAI output (summary of actions taken):**
Before writing code, the AI asked two clarifying questions via a structured choice prompt, since the
request as stated left two protocol-shape decisions genuinely open: (1) how should the server obtain the
"correct" hash to check an upload against — piggybacked directly on the `STOR` command's own argument
list (recommended, one round trip, no new command) vs. a separate follow-up command after the existing
`226`; and (2) should the requested NAT-keepalive mechanism cover PASSIVE mode only (matching what the
student actually hit) or both PASSIVE and ACTIVE. The student answered: hashing should trigger
automatically whenever `[integrity] verify=true`, with the server dropping the file and telling the user
on failure (confirming the piggyback-on-`STOR` design); and keepalive scoped to PASSIVE only, client-driven.

Implemented across four files:
- `common.py`: added `Reply.INTEGRITY_FAILED = "552 Integrity check failed; uploaded file discarded."`
- `server.py`: `tcp_sock.listen(5 if THREADING_MODE == "thread" else 1)` → `tcp_sock.listen(128)`, with a
  comment explaining `listen()`'s backlog only bounds not-yet-`accept()`ed connections, not concurrent
  clients — raising it is safe regardless of threading mode. `handle_stor()` now parses an optional
  trailing `<algorithm> <hash>` off the `STOR` argument list; after writing the file, if a hash was
  supplied it recomputes the hash of what was actually written, and on mismatch `os.remove()`s the file
  and replies `552` instead of `226` (unrecognized algorithm names are treated as a mismatch rather than
  raising).
- `client.py`: `put()` now computes the hash of what the server will actually persist (post
  `decode_ascii_transfer()` round-trip for TYPE A) *before* sending `STOR`, and appends it to the command
  only when `VERIFY_HASH` is set — so `config.ini` deleted/`verify=false` reproduces the exact old wire
  format. The old post-upload client-side `HASH` round-trip in `put()` was removed as redundant (the
  server's own `226`/`552` reply is now the verdict); `get()`'s client-side verify is untouched, since the
  client is still the one persisting the downloaded bytes. Separately, added a background daemon thread
  (`_keepalive_loop`/`_start_keepalive`/`_stop_keepalive`) that, only while `data_mode == PASSIVE` and no
  transfer is in flight (`_data_busy` flag, set around the actual UDP send/receive phases of
  `put()`/`_recv_data_payload()`), resends the same `PKT_HELLO` datagram to the passive target every
  `[client] keepalive_interval` seconds (new config key, default 15, PASSIVE-only, no server-side
  counterpart needed). Started on `set_passive()`, stopped on `set_active()`/`set_fixed()`/`close()`.
- `config.ini`: documented and added `keepalive_interval = 15` under `[client]`.

**Refinement & problem solving:**
- **Asked before designing, rather than guessing the protocol shape.** The request text alone didn't
  specify how the server would learn the expected hash — the AI surfaced this as an explicit either/or
  choice instead of silently picking one, since getting it wrong would have meant reworking both ends of
  the wire format later.
- **Verified server-side deletion with an adversarial test, not just the happy path.** Set up a disposable
  loopback `config.ini` (`mode=gbn`, `verify=true`, `keepalive_interval=2`), started a real server, then
  drove `FTPClient` directly from a throwaway script that monkey-patched `compute_hash()` to return a
  deliberately wrong digest for one `put()` call. Confirmed: the server's log recorded the mismatch with
  both the expected and actual hashes, the file was absent from `server_storage/` afterward, and the client
  printed the new `552` reply — not just that no exception was raised.
- **Verified the normal path separately**: a real `put`+`get` round trip with a correct hash returned `226`
  both times and printed `Integrity verified ... MATCH` for the download side.
- **Verified the keepalive doesn't corrupt a real transfer that follows it** — idled a live PASSIVE session
  for 6 seconds (three keepalive ticks at the 2-second test interval) and then immediately ran a real
  `put()`; it succeeded on the first attempt with no GBN retransmits logged, confirming the existing
  `drain_stale_packets()` call (already present before every `STOR`/`RETR`/`LIST`, added for an unrelated
  reason in an earlier session) silently absorbs the queued keepalive datagrams rather than being misread
  as transfer data.
- **Rejected adding a new packet type or any server-side keepalive logic.** The server never needs to
  distinguish a keepalive `PKT_HELLO` from any other stray datagram — it already either hasn't started
  listening on that socket yet (queues harmlessly in the OS receive buffer) or is between transfers
  (drained). Reusing the existing packet type and existing drain call kept this additive with zero changes
  to `server.py`'s data-channel code path.
- **Test hygiene**: backed up the student's real `config.ini` (which carries a live Azure `advertise_ip`
  and passive port range unsuitable for loopback testing) before swapping in a throwaway loopback config,
  restored it afterward, and inspected `git diff config.ini` to confirm only the intended
  `keepalive_interval` addition survived — same discipline as Entry 12.
- `[student to fill in]`: re-derive, without notes, why the integrity hash rides on `STOR`'s own argument
  line instead of a separate command, and why that's *not* the kind of protocol-negotiation the project
  deliberately avoids for `[reliability] mode` (see `CLAUDE.md`) — the distinction is that this is
  optional/tolerant (a `STOR` with no hash behaves exactly as before), not a mandatory handshake both ends
  must agree on ahead of time.
- `[student to fill in]`: explain why the NAT keepalive only needed to be implemented client-side for
  PASSIVE mode, tied to which end's HELLO datagram actually creates the NAT/router UDP mapping in the
  first place — this is a natural oral-defense follow-up to the existing PASV/PORT theory questions in
  Entry 11.

---
