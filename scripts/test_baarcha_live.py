import os
import sys
import json
import base64
import urllib.request
import asyncio
import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_KEY = os.environ.get("BAARCHA_API_KEY", "pk_live_HG2nbAPicXKpZqhpgW4MxRcJ")

async def test_baarcha_live(prompt="عسلامة، شكونك إنت وفاش تنجم تعاوني؟", voice="dido"):
    print("=" * 60)
    print(" TESTING PUNICA LIVE (baarcha-live) ")
    print("=" * 60)
    
    # 1. Create session via POST /api/v1/live/sessions
    print("\n1. Requesting a Live Session for model 'baarcha-live'...")
    req = urllib.request.Request(
        "https://punica.tn/api/v1/live/sessions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        },
        data=json.dumps({"model": "baarcha-live"}).encode("utf-8")
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            session = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Error creating session: {e}")
        return

    ws_url = session["url"]
    ticket = session["client_secret"]["value"]
    print(f"Session established!")
    print(f"WebSocket URL: {ws_url}")
    print(f"Session rate: {session.get('rate')}")

    # 2. Connect to the WebSocket stream
    subprotocols = ["baarcha-live", f"ticket.{ticket}"]
    print(f"\n2. Connecting to WebSocket stream...")

    async with websockets.connect(ws_url, subprotocols=subprotocols) as ws:
        print("Connected to Baarcha Live WebSocket!")

        # 3. Send prompt
        msg = {
            "type": "text",
            "model": "baarcha-live",
            "voice": voice,
            "message": prompt
        }
        print(f"Sending prompt: \"{prompt}\" (Voice: {voice})")
        print("\nStreaming response from baarcha-live:")
        print("-" * 40)
        await ws.send(json.dumps(msg))

        received_text = []
        audio_chunks = []

        async for data in ws:
            # Binary frames from WebSocket
            if isinstance(data, bytes):
                audio_chunks.append(data)
                continue

            event = json.loads(data)
            evt_type = event.get("type")

            if evt_type == "error":
                print(f"\n[Error from Punica]: {event.get('message')}")
                break

            # Text delta
            delta = event.get("delta")
            if delta:
                print(delta, end="", flush=True)
                received_text.append(delta)

            # WAV audio inside JSON if provided
            if "audio" in event and event["audio"].get("wav_b64"):
                audio_chunks.append(base64.b64decode(event["audio"]["wav_b64"]))

            # Reply completed
            if event.get("done"):
                print("\n" + "-" * 40)
                print("[Done receiving reply]")
                await ws.send(json.dumps({"type": "end"}))

            if evt_type == "session.ended":
                usage = event.get("usage", {})
                print(f"Session ended. Billed seconds: {usage.get('billable_seconds', 0):.2f}s")
                break

    # Save audio if received
    if audio_chunks:
        output_wav = "baarcha_live_reply.wav"
        with open(output_wav, "wb") as f:
            for chunk in audio_chunks:
                f.write(chunk)
        print(f"\nSaved synthesized response audio to: {output_wav}")

    print("\n" + "=" * 60)
    print("SUCCESS: baarcha-live tested successfully!")
    print("=" * 60)

if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "عسلامة، شكونك إنت وفاش تنجم تعاوني؟"
    voice = sys.argv[2] if len(sys.argv) > 2 else "dido"
    asyncio.run(test_baarcha_live(prompt, voice))
