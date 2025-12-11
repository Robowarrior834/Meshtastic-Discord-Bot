import asyncio
import json
import logging
import queue
import sys
import time
from datetime import datetime, timezone
import os
import discord
from discord import app_commands, ButtonStyle
from discord.ui import View, Button
import meshtastic
import meshtastic.serial_interface
import pytz
from pubsub import pub
import aiohttp
import zipcodes  # pip install zipcodes

# ---------------- Config ----------------
def load_config():
    try:
        with open("config.json", "r") as f:
            config = json.load(f)
            config["channel_names"] = {int(k): v for k, v in config["channel_names"].items()}
            return config
    except Exception as e:
        logging.critical(f"Error loading config.json: {e}")
        raise

config = load_config()
TOKEN = config["discord_bot_token"]
CHANNEL_ID = int(config["discord_channel_id"])
CHANNEL_NAMES = config["channel_names"]
TIME_ZONE = config["time_zone"]
MESHTASTIC_PORT = config["SerialDevice"]
WX_FILE = config.get("wx_file")

COLOR = 0x67ea94  # Meshtastic green

# ---------------- Queues ----------------
meshtodiscord = queue.Queue()
discordtomesh = queue.Queue()
nodelistq = queue.Queue()

# ---------------- Helper Functions ----------------
def get_long_name(node_id, nodes):
    try:
        # nodes keys may be ints or strings; try both if necessary
        if node_id in nodes:
            return nodes[node_id]['user'].get('longName', 'Unknown')
        # fallback: try string/int conversions
        try:
            alt_key = int(node_id)
            if alt_key in nodes:
                return nodes[alt_key]['user'].get('longName', 'Unknown')
        except Exception:
            pass
        try:
            alt_key = str(node_id)
            if alt_key in nodes:
                return nodes[alt_key]['user'].get('longName', 'Unknown')
        except Exception:
            pass
    except Exception:
        pass
    return 'Unknown'

def onConnectionMesh(interface, topic=pub.AUTO_TOPIC):
    logging.info(f"Connected to mesh: {interface.myInfo}")

def onReceiveMesh(packet, interface):
    """
    Handles received Meshtastic packets and queues a Discord embed for posting.
    Uses nodes[packet['fromId']]['hopsAway'] to show real hop distance.
    """
    try:
        if 'decoded' not in packet:
            logging.warning(f"Received packet without 'decoded': {packet}")
            return

        # Only handle text messages (TEXT_MESSAGE_APP)
        if packet['decoded'].get('portnum') != 'TEXT_MESSAGE_APP':
            return

        # channel index might be under packet['channel'] or decoded
        channel_index = packet.get('channel', packet['decoded'].get('channel', 0))
        channel_name = CHANNEL_NAMES.get(channel_index, f"Unknown Channel ({channel_index})")

        nodes = interface.nodes if hasattr(interface, 'nodes') else {}
        from_long = get_long_name(packet.get('fromId'), nodes)
        to_long = 'All Nodes' if packet.get('toId') == '^all' else get_long_name(packet.get('toId'), nodes)

        # Determine real hop distance (hopsAway) from node DB
        hops_away = '?'
        try:
            node_key = packet.get('fromId')
            # Try multiple key formats
            if node_key in nodes:
                hops_away = nodes[node_key].get('hopsAway', '?')
            else:
                try:
                    ik = int(node_key)
                    if ik in nodes:
                        hops_away = nodes[ik].get('hopsAway', '?')
                except Exception:
                    sk = str(node_key)
                    if sk in nodes:
                        hops_away = nodes[sk].get('hopsAway', '?')
        except Exception:
            hops_away = '?'

        # Also capture SNR, lastHeard if available
        snr = '?'
        last_heard_str = "Unknown"
        try:
            if node_key in nodes:
                snr = nodes[node_key].get('snr', '?')
                lh = nodes[node_key].get('lastHeard', 0)
                if lh:
                    last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
            else:
                # Try conversions again for SNR/lastHeard
                try:
                    ik = int(node_key)
                    if ik in nodes:
                        snr = nodes[ik].get('snr', '?')
                        lh = nodes[ik].get('lastHeard', 0)
                        if lh:
                            last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
                except Exception:
                    sk = str(node_key)
                    if sk in nodes:
                        snr = nodes[sk].get('snr', '?')
                        lh = nodes[sk].get('lastHeard', 0)
                        if lh:
                            last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
        except Exception:
            pass

        logging.info(f"Received message from {from_long} to {to_long}, channel {channel_name}, hopsAway={hops_away}, snr={snr}")

        # Build embed
        embed = discord.Embed(
            title="Message Received",
            description=packet['decoded'].get('text', ''),
            color=COLOR
        )
        embed.add_field(name="From Node", value=f"{from_long} ({packet.get('fromId')})", inline=True)
        if packet.get('toId') == '^all':
            embed.add_field(name="To Channel", value=channel_name, inline=True)
        else:
            embed.add_field(name="To Node", value=f"{to_long} ({packet.get('toId')})", inline=True)

        embed.add_field(name="Hops Away", value=str(hops_away), inline=True)
        embed.add_field(name="SNR", value=str(snr), inline=True)
        embed.add_field(name="Last Heard", value=last_heard_str, inline=True)

        embed.set_footer(text=datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p'))

        meshtodiscord.put(embed)

    except Exception as e:
        logging.warning(f"Error in onReceiveMesh: {e}", exc_info=True)

# ---------------- Meshtastic Text Chunking ----------------
def send_text_chunks(text, author_name=None, destinationId="^all", channelIndex=0, max_bytes=200):
    if not client.iface:
        return []
    
    full_text = f"{author_name}: {text}" if author_name else text
    sent_chunks = []

    encoded = full_text.encode('utf-8')
    if len(encoded) <= max_bytes:
        client.iface.sendText(full_text, destinationId=destinationId, channelIndex=channelIndex)
        sent_chunks.append(full_text)
        time.sleep(1.5)
    else:
        start = 0
        while start < len(encoded):
            end = start + max_bytes
            chunk_bytes = encoded[start:end]

            while True:
                try:
                    chunk = chunk_bytes.decode('utf-8')
                    break
                except UnicodeDecodeError:
                    chunk_bytes = chunk_bytes[:-1]

            client.iface.sendText(chunk, destinationId=destinationId, channelIndex=channelIndex)
            sent_chunks.append(chunk)
            time.sleep(1.5)
            start += len(chunk.encode('utf-8'))

    return sent_chunks

# ---------------- MeshBot ----------------
class MeshBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)
        self.iface = None

    async def setup_hook(self):
        self.bg_task = self.loop.create_task(self.background_task())
        await self.tree.sync()

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} (ID: {self.user.id})")

    async def background_task(self):
        await self.wait_until_ready()
        counter = 0
        nodelist_chunks = []
        public_channel = self.get_channel(CHANNEL_ID)

        pub.subscribe(onReceiveMesh, "meshtastic.receive")
        pub.subscribe(onConnectionMesh, "meshtastic.connection.established")

        while not self.is_closed():
            if self.iface is None:
                try:
                    def connect_iface():
                        try:
                            return meshtastic.serial_interface.SerialInterface(MESHTASTIC_PORT)
                        except SystemExit:
                            logging.error(f"Meshtastic failed to open {MESHTASTIC_PORT}")
                            return None
                    self.iface = await asyncio.get_running_loop().run_in_executor(None, connect_iface)
                    if self.iface:
                        logging.info("Connected to Meshtastic interface")
                    else:
                        await asyncio.sleep(5)
                        continue
                except Exception as e:
                    logging.error(f"Error connecting to Meshtastic: {e}")
                    await asyncio.sleep(5)
                    continue

            counter += 1
            if counter % 12 == 1:
                # Build nodelist for the last 15 minutes
                nodelist = ["**Nodes seen in last 6 Hours:**"]
                nodes = self.iface.nodes if hasattr(self.iface, 'nodes') else {}
                sorted_nodes = sorted(nodes.keys(), key=lambda n: nodes[n].get("hopsAway", 999))

                for node in sorted_nodes:
                    try:
                        user = nodes[node].get('user', {})
                        last_heard = nodes[node].get('lastHeard', 0)
                        if time.time() - last_heard <= 6 * 60 * 60:
                            last_heard_str = datetime.fromtimestamp(last_heard, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p') if last_heard else "Unknown"
                            nodelist.append(
                                f"**ID:** {user.get('id', node)} | "
                                f"**Name:** {user.get('longName', 'Unknown')} | "
                                f"**HopsAway:** {nodes[node].get('hopsAway', '?')} | "
                                f"**SNR:** {nodes[node].get('snr', '?')} | "
                                f"**Last Heard:** {last_heard_str}"
                            )
                    except Exception:
                        pass

                # --- Chunk into groups of 20 lines ---
                chunk_size = 10
                nodelist_chunks = [ "\n".join(nodelist[i:i + chunk_size]) for i in range(0, len(nodelist), chunk_size) ]

            # Process mesh -> Discord (embeds queued by onReceiveMesh)
            try:
                embed = meshtodiscord.get_nowait()
                if isinstance(embed, discord.Embed) and public_channel:
                    await public_channel.send(embed=embed)
                meshtodiscord.task_done()
            except queue.Empty:
                pass

            # Process Discord -> mesh (messages queued by commands)
            try:
                msg_data = discordtomesh.get_nowait()
                text = msg_data['text']
                destinationId = msg_data.get('dest', '^all')
                channelIndex = msg_data.get('channel', 0)
                author_name = msg_data.get('author')

                sent_chunks = []
                if self.iface:
                    sent_chunks = send_text_chunks(text, author_name=author_name, destinationId=destinationId, channelIndex=channelIndex)

                if public_channel and sent_chunks:
                    embed = discord.Embed(
                        title=f"Meshtastic Message{' by ' + author_name if author_name else ''}",
                        description="\n".join(sent_chunks),
                        color=COLOR
                    )
                    embed.set_footer(text=datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p'))
                    await public_channel.send(embed=embed)

                discordtomesh.task_done()
            except queue.Empty:
                pass

            # Process ephemeral node list requests (/active)
            try:
                interaction = nodelistq.get_nowait()
                if not nodelist_chunks:
                    await interaction.followup.send("No nodes seen in the last 15 minutes.", ephemeral=True)
                else:
                    for chunk in nodelist_chunks:
                        await interaction.followup.send(chunk, ephemeral=True)
                nodelistq.task_done()
            except queue.Empty:
                pass

            await asyncio.sleep(5)


# ---------------- Help View ----------------
class HelpView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="Robowarrior", style=ButtonStyle.link, url="https://github.com/Robowarrior834/Meshtastic-Discord-Bot/tree/main"))
        self.add_item(Button(label="Meshtastic", style=ButtonStyle.link, url="https://meshtastic.org"))

# ---------------- Bot Setup ----------------
intents = discord.Intents.default()
client = MeshBot(intents=intents)

# ---------------- Helper to queue message ----------------
def queue_meshtastic_message(author_name, text, dest="^all", channel=0):
    discordtomesh.put({'text': text, 'dest': dest, 'channel': channel, 'author': author_name})
    logging.debug(f"Queued message from {author_name} to Meshtastic: {text}")

# ---------------- Standard Commands ----------------
@client.tree.command(name="help", description="Shows the help message.")
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    help_text = ("**Command List**\n"
                 "`/sendid` - Send a message to another node.\n"
                 "`/sendnum` - Send a message to another node.\n"
                 "`/help` - Shows this help message.\n"
                 "`/wxnow` - Shows current weather.\n"
                 "`/longfast` - Sends a long fast message.\n"
                 "`/active` - List active nodes seen in the last 15 minutes.\n")
    for idx, name in CHANNEL_NAMES.items():
        if name.lower() not in ["longfast", "wxnow"]:
            help_text += f"`/{name.lower()}` - Send a message in the {name} channel.\n"
    embed = discord.Embed(title="Meshtastic Bot Help", description=help_text, color=COLOR)
    embed.set_footer(text="Meshtastic Discord Bot by Robowarrior")
    embed.set_image(url="https://i.imgur.com/qvo2NkW.jpeg")
    await interaction.followup.send(embed=embed, view=HelpView(), ephemeral=True)

# ---------- /sendid ----------
@client.tree.command(name="sendid", description="Send a message to a specific node (hex ID).")
async def sendid(interaction: discord.Interaction, nodeid: str, message: str):
    try:
        if nodeid.startswith("!"):
            nodeid = nodeid[1:]
        nodenum = int(nodeid, 16)
        username = interaction.user.name
        queue_meshtastic_message(username, message, dest=nodenum)
        await interaction.response.send_message(f"Message queued to node !{nodeid}.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid hexadecimal node ID.", ephemeral=True)

# ---------- /sendnum ----------
@client.tree.command(name="sendnum", description="Send a message to a specific node (decimal ID).")
async def sendnum(interaction: discord.Interaction, nodenum: int, message: str):
    username = interaction.user.name
    queue_meshtastic_message(username, message, dest=nodenum)
    await interaction.response.send_message(f"Message queued to node {nodenum}.", ephemeral=True)

# ---------- Dynamic Channel Commands ----------
def create_channel_command(channel_index: int, channel_name: str):
    # keep reserved names out
    if channel_name.lower() in ["longfast", "wxnow", "help", "noaa_alerts", "sendid", "sendnum", "active"]:
        return

    @client.tree.command(
        name=channel_name.lower(),
        description=f"Send a message to the {channel_name} channel."
    )
    async def send_channel(interaction: discord.Interaction, message: str):
        username = interaction.user.name
        queue_meshtastic_message(username, message, channel=channel_index)
        await interaction.response.send_message(f"Message queued to channel {CHANNEL_NAMES[channel_index]}.", ephemeral=True)

for idx, name in CHANNEL_NAMES.items():
    create_channel_command(idx, name)

# ---------- /longfast ----------
@client.tree.command(name="longfast", description="Send a long fast message to all nodes.")
async def longfast(interaction: discord.Interaction, message: str):
    username = interaction.user.name
    queue_meshtastic_message(username, message)
    await interaction.response.send_message("Message queued to all nodes.", ephemeral=True)

# ---------- /noaa_alerts ----------
async def fetch_noaa_alerts(zip_code: str):
    try:
        loc = zipcodes.matching(zip_code)
        if not loc:
            return [f"Invalid ZIP code: {zip_code}"], []

        lat, lon = loc[0]['lat'], loc[0]['long']
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return [f"Error fetching NOAA alerts (status {resp.status})"], []
                data = await resp.json()

        discord_alerts, mesh_alerts = [], []
        now = datetime.now(timezone.utc)
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            status = props.get("status", "")
            event = props.get("event", "")
            headline = props.get("headline", "")
            description = props.get("description", "")
            ends = props.get("ends", "")
            severity = props.get("severity", "")
            urgency = props.get("urgency", "")

            if status != "Actual":
                continue

            ends_dt = datetime.fromisoformat(ends).astimezone(timezone.utc) if ends else None
            if ends_dt and ends_dt < now:
                continue

            if ("winter" in event.lower() or severity in ["Severe", "Extreme"] or urgency in ["Expected", "Immediate"]):
                discord_alerts.append(f"**{event} for ZIP {zip_code}**: {headline}\n{description}")
                end_time_str = ends_dt.strftime('%I:%M %p') if ends_dt else "unknown"
                mesh_alerts.append(f"**{event} for ZIP {zip_code}** until {end_time_str}")

        if not discord_alerts:
            discord_alerts = [f"No active/severe or winter weather alerts for ZIP {zip_code}."]
            mesh_alerts = []

        return discord_alerts, mesh_alerts

    except Exception as e:
        logging.error(f"Error fetching NOAA alerts for ZIP {zip_code}: {e}", exc_info=True)
        return [f"Error fetching NOAA alerts: {e}"], []

@client.tree.command(name="noaa_alerts", description="Get active/severe NOAA alerts for a ZIP code.")
@app_commands.describe(send_to_mesh="Send NOAA alerts to Meshtastic as well")
async def noaa_alerts_command(interaction: discord.Interaction, zip_code: str, send_to_mesh: bool = False):
    logging.info(f"/noaa_alerts command from {interaction.user.name}, zip={zip_code}, send_to_mesh={send_to_mesh}")
    await interaction.response.defer(ephemeral=False)
    username = interaction.user.name
    discord_alerts, mesh_alerts = await fetch_noaa_alerts(zip_code)

    if send_to_mesh:
        for line in mesh_alerts:
            queue_meshtastic_message(username, line)

    footer_text = f"Requested by {username} at {datetime.now().strftime('%d %B %Y %I:%M:%S %p')}"
    if send_to_mesh:
        footer_text += " | Also sent to Meshtastic"

    embed = discord.Embed(
        title=f"NOAA Alerts for ZIP {zip_code}",
        description="\n".join(discord_alerts),
        color=0xff0000
    )
    embed.set_footer(text=footer_text)
    await interaction.followup.send(embed=embed)

# ---------- /wxnow ----------
@client.tree.command(name="wxnow", description="Shows the current weather report from wxnow file.")
@app_commands.describe(send_to_mesh="Send weather info to Meshtastic as well")
async def wxnow_command(interaction: discord.Interaction, send_to_mesh: bool = False):
    logging.info(f"/wxnow command from {interaction.user.name}, send_to_mesh={send_to_mesh}")
    await interaction.response.defer(ephemeral=False)

    if not WX_FILE or not os.path.exists(WX_FILE):
        logging.warning("WX_FILE not found or not configured")
        await interaction.followup.send("Weather file not found or not configured.", ephemeral=False)
        return

    try:
        with open(WX_FILE, "r") as f:
            lines = f.read().splitlines()

        if len(lines) < 2:
            report_lines = ["No weather report available."]
        else:
            date_line = lines[0].strip()
            data_line = lines[1].strip()
            wind_dir = int(data_line[0:3])
            wind_speed = int(data_line[4:7])
            wind_gust = int(data_line[data_line.find('g')+1:data_line.find('g')+4])
            temp = int(data_line[data_line.find('t')+1:data_line.find('t')+4])
            rain_hour = int(data_line[data_line.find('r')+1:data_line.find('r')+3])/100.0
            rain_24h = int(data_line[data_line.find('p')+1:data_line.find('p')+3])/100.0
            rain_since_midnight = int(data_line[data_line.find('P')+1:data_line.find('P')+3])/100.0
            humidity_raw = int(data_line[data_line.find('h')+1:data_line.find('h')+3])
            humidity = 100 if humidity_raw == 0 else humidity_raw
            baro_raw = int(data_line[data_line.find('b')+1:data_line.find('b')+5])
            baro = baro_raw / 10.0

            report_lines = [
                f"Flint Hill Weather Report:",
                f"Date/Time: {date_line}",
                f"Wind: {wind_dir}° at {wind_speed} mph (Gust {wind_gust} mph)",
                f"Temp: {temp}°F",
                f"Rain last hour: {rain_hour} in",
                f"Rain last 24h: {rain_24h} in",
                f"Rain since midnight: {rain_since_midnight} in",
                f"Humidity: {humidity}%",
                f"Pressure: {baro} mb"
            ]

        # Send to Discord
        embed = discord.Embed(
            title="Flint Hill Weather Report",
            description="\n".join(report_lines),
            color=COLOR
        )
        footer_text = datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
        if send_to_mesh:
            footer_text += " | Also sent to Meshtastic"
        embed.set_footer(text=footer_text)
        await interaction.followup.send(embed=embed, ephemeral=False)

        # Send line-by-line to Meshtastic if requested
        if send_to_mesh and client.iface and hasattr(client.iface, "myInfo") and client.iface.myInfo:
            logging.info("Sending weather report to Meshtastic")
            def send_lines():
                for line in report_lines:
                    client.iface.sendText(line, destinationId="^all", channelIndex=0)
                    time.sleep(3)
            await asyncio.get_running_loop().run_in_executor(None, send_lines)

    except Exception as e:
        logging.error(f"WXnow error: {e}", exc_info=True)
        await interaction.followup.send(f"Error reading WXnow file: {e}", ephemeral=False)

# ---------- /active ----------
@client.tree.command(name="active", description="List all active nodes seen in the last 15 minutes.")
async def active_nodes(interaction: discord.Interaction):
    """
    Puts the interaction into the nodelistq; the background task will respond with ephemeral chunks.
    """
    await interaction.response.defer(ephemeral=True)
    nodelistq.put(interaction)

# ---------------- Run Bot ----------------
def run_bot():
    try:
        client.run(TOKEN)
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
    finally:
        # attempt graceful close
        try:
            asyncio.run(client.close())
        except Exception:
            pass

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_bot()
