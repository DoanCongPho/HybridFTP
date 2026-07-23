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
