# Hybrid FTP — Diagrams

Source diagrams for the Technical Report (`docs/technical_report.tex`), Sections 1 and 3.
Rendered natively if viewed on GitHub/GitLab; export to PNG/SVG for LaTeX embedding via
`docs/render_diagrams.sh` (uses `@mermaid-js/mermaid-cli`).

## 1. Sequence diagrams (Report Section 1)

### 1.1 Full lifecycle — login, FIXED-mode STOR with a Go-Back-N retransmission

Covers: TCP control-channel handshake, the FIXED-mode `HELLO` address-learning
mechanism, and one simulated packet loss recovered by the Go-Back-N reliable-UDP
layer (`[reliability] mode = gbn`) — the sender retransmits its whole in-flight
window after a timeout, exactly as implemented in `common.gbn_send()`.

```mermaid
sequenceDiagram
    participant C as Client (FTPClient)
    participant S as Server (Session)

    Note over C,S: --- Control channel setup (TCP, port 2121) ---
    C->>S: TCP connect()
    S-->>C: 220 Service ready.
    C->>S: USER alice
    S-->>C: 331 Username OK, need password.
    C->>S: PASS password123
    S-->>C: 230 Login successful.

    Note over C,S: --- Data-channel handshake (UDP, FIXED mode) ---
    C->>S: PKT_HELLO (UDP, to DATA_PORT 2122)
    Note right of S: session.client_data_addr learned<br/>from HELLO's source address

    Note over C,S: --- Upload (STOR), reliability mode = gbn ---
    C->>S: STOR photo.jpg
    S-->>C: 150 File status okay, opening data connection.

    par Go-Back-N sliding window (window_size = 4)
        C->>S: PKT_DATA seq=0
        C->>S: PKT_DATA seq=1
        C->>S: PKT_DATA seq=2
        C->>S: PKT_DATA seq=3
    end
    S-->>C: PKT_ACK seq=0
    Note right of S: seq=1 lost in transit —<br/>never arrives
    S-->>C: PKT_ACK seq=0 (re-ACK: seq=1 still expected)

    Note over C: RTO expires waiting for ACK ≥ base(=1)
    Note over C: Go-Back-N: retransmit WHOLE window from base
    C->>S: PKT_DATA seq=1 (retransmit)
    C->>S: PKT_DATA seq=2 (retransmit)
    C->>S: PKT_DATA seq=3 (retransmit)
    S-->>C: PKT_ACK seq=3
    C->>S: PKT_FIN seq=4
    S-->>C: PKT_ACK seq=4
    Note over C,S: gbn_send() returns True — FIN itself was acknowledged

    S-->>C: 226 Transfer complete.

    Note over C,S: --- Integrity verification (optional, [integrity] verify) ---
    C->>S: HASH photo.jpg
    S-->>C: 213 sha256 <hexdigest>
    Note left of C: compare against local SHA-256 of the same bytes → MATCH

    C->>S: QUIT
    S-->>C: 221 Goodbye.
```

### 1.2 Active/Passive mode negotiation (Advanced Level)

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server

    rect rgb(235, 245, 255)
    Note over C,S: PASSIVE — server opens the socket, client connects to it
    C->>S: PASV
    S->>S: handle_pasv(): bind new ephemeral UDP socket
    S-->>C: 227 Entering Passive Mode (h1,h2,h3,h4,p1,p2)
    C->>S: PKT_HELLO (UDP, to server's announced port)
    Note right of S: client_data_addr learned from HELLO
    end

    rect rgb(255, 245, 235)
    Note over C,S: ACTIVE — client opens the socket, tells server its address
    C->>S: PORT h1,h2,h3,h4,p1,p2
    S->>S: handle_port(): bind new ephemeral UDP socket
    Note right of S: client_data_addr known immediately<br/>from the PORT argument — no HELLO needed
    S-->>C: 200 PORT command successful - server data port N.
    Note left of C: deliberate RFC 959 deviation — UDP has no<br/>server-initiated connect(), so the client needs<br/>the server's port for its own uploads (STOR)
    end
```

## 2. Flowcharts (Report Section 3)

### 2.1 Server thread-dispatch logic (`server.py: main()`)

```mermaid
flowchart TD
    A[main: bind TCP 2121 + UDP 2122] --> B{tcp_sock.accept}
    B --> C[New connection: conn, addr]
    C --> D{config threading == 'thread'?}
    D -- yes --> E["threading.Thread(target=serve, daemon=True).start()"]
    D -- no --> F["serve(conn, addr) — synchronous, blocks accept loop"]
    E --> G[handle_client in its own thread]
    F --> G
    G --> H[Session created, registered in _active_sessions]
    H --> I["while True: recv_line(conn) → dispatch command"]
    I -->|QUIT / EOF / timeout / error| J[cleanup_session: close sockets, unregister]
    J --> B
```

### 2.2 Go-Back-N sender state machine (`common.gbn_send()`)

```mermaid
flowchart TD
    A[Frame data into PKT_DATA 0..N-1 + PKT_FIN N] --> B["base=0, next_seq=0"]
    B --> C["send_window(): sendto up to window_size packets"]
    C --> D{"base < total?"}
    D -- no --> Z["return True (FIN acknowledged)"]
    D -- yes --> E["recvfrom() with timeout = rto"]
    E -->|PKT_ACK ack_seq, from dest_addr| F{"ack_seq >= base?"}
    F -- yes --> G["base = ack_seq + 1; retries = 0"]
    G --> H[send_window: fill window with any newly-eligible packets]
    H --> D
    F -- no --> E
    E -->|socket.timeout| I["retries += 1"]
    I --> J{"retries > max_retries?"}
    J -- yes --> Y["return False (peer presumed gone)"]
    J -- no --> K["Resend ALL packets base..next_seq-1<br/>(whole window — Go-Back-N, not Selective Repeat)"]
    K --> D
```

### 2.3 Go-Back-N receiver state machine (`common.gbn_receive()`)

```mermaid
flowchart TD
    A["expected_seq = 0, chunks = []"] --> B["recvfrom() with timeout = rto"]
    B -->|timeout| C["idle += 1"]
    C --> D{"idle > max_retries?"}
    D -- yes --> Y[return None]
    D -- no --> B
    B -->|packet from expected_addr| E{"valid checksum AND<br/>seq == expected_seq?"}
    E -- yes --> F{"pkt_type?"}
    F -- PKT_DATA --> G["chunks.append(payload)<br/>send PKT_ACK(expected_seq)<br/>expected_seq += 1"]
    G --> B
    F -- PKT_FIN --> H["send PKT_ACK(expected_seq)<br/>return join(chunks)"]
    E -- no --> I{"expected_seq > 0?"}
    I -- yes --> J["Re-send PKT_ACK(expected_seq - 1)<br/>(corrupt / out-of-order / duplicate retransmit)"]
    J --> B
    I -- no --> B
```

### 2.4 Client data-mode selection (`FTPClient.login()`)

```mermaid
flowchart TD
    A["login(): PASS accepted (230)"] --> B{"self.data_mode"}
    B -- FIXED --> C["Send PKT_HELLO to (host, DATA_PORT)"]
    B -- ACTIVE --> D["set_active(): send PORT with own ip:port<br/>parse server's port from 200 reply"]
    B -- PASSIVE --> E["set_passive(): send PASV<br/>parse 227 reply, HELLO that address"]
    C --> F[Ready for put/get]
    D --> F
    E --> F
    F --> G["REPL: active / passive / fixed<br/>can still switch mode live, mid-session"]
```

## 3. Regenerating exported images

```bash
docs/render_diagrams.sh   # writes docs/diagrams/*.png for LaTeX \includegraphics
```
