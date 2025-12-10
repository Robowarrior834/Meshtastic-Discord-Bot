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
MESHTASTIC_PORT = "/dev/ttyACM0"
WX_FILE = config.get("wx_file")

COLOR = 0x67ea94  # Meshtastic green

# ---------------- Queues ----------------
meshtodiscord = queue.Queue()
discordtomesh = queue.Queue()
nodelistq = queue.Queue()

# ---------------- Helper Functions ----------------
def get_long_name(node_id, nodes):
    if node_id in nodes:
        return nodes[node_id]['user'].get('longName', 'Unknown')
    return 'Unknown'

def onConnectionMesh(interface, topic=pub.AUTO_TOPIC):
    logging.info(f"Connected to mesh: {interface.myInfo}")

def onReceiveMesh(packet, interface):
    try:
        if 'decoded' not in packet:
            logging.warning(f"Received packet without 'decoded': {packet}")
            return

        if packet['decoded'].get('portnum') != 'TEXT_MESSAGE_APP':
            return

        channel_index = packet.get('channel', packet['decoded'].get('channel', 0))
        channel_name = CHANNEL_NAMES.get(channel_index, f"Unknown Channel ({channel_index})")

        nodes = interface.nodes
        from_long = get_long_name(packet['fromId'], nodes)
        to_long = 'All Nodes' if packet['toId'] == '^all' else get_long_name(packet['toId'], nodes)

        embed = discord.Embed(
            title="Message Received",
            description=packet['decoded'].get('text', ''),
            color=COLOR
        )
        embed.add_field(name="From Node", value=f"{from_long} ({packet['fromId']})", inline=True)
        if packet['toId'] == '^all':
            embed.add_field(name="To Channel", value=channel_name, inline=True)
        else:
            embed.add_field(name="To Node", value=f"{to_long} ({packet['toId']})", inline=True)
        embed.set_footer(text=datetime.now().strftime('%d %B %Y %I:%M:%S %p'))

        meshtodiscord.put(embed)

    except Exception as e:
        logging.warning(f"Error in onReceiveMesh: {e}")

# ---------------- Meshtastic Text Chunking ----------------
def send_text_chunks(text, chunk_size=50, destinationId="^all", channelIndex=0):
    """
    Split text into smaller chunks and send each over Meshtastic.
    """
    if not client.iface:
        return
    lines = text.splitlines()
    for line in lines:
        for i in range(0, len(line), chunk_size):
            chunk = line[i:i+chunk_size]
            client.iface.sendText(chunk, destinationId=destinationId, channelIndex=channelIndex)
            time.sleep(1.5)

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
                nodelist = ["**Nodes seen in last 15 minutes:**\n"]
                nodes = self.iface.nodes
                for node in nodes:
                    try:
                        user = nodes[node].get('user', {})
                        last_heard = nodes[node].get('lastHeard', 0)
                        if time.time() - last_heard <= 15 * 60:
                            last_heard_str = datetime.fromtimestamp(last_heard, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p') if last_heard else "Unknown"
                            nodelist.append(
                                f"**ID:** {user.get('id', node)} | **Name:** {user.get('longName', 'Unknown')} | "
                                f"**Hops:** {nodes[node].get('hopsAway', 0)} | "
                                f"**SNR:** {nodes[node].get('snr', '?')} | "
                                f"**Last Heard:** {last_heard_str}"
                            )
                    except Exception:
                        pass
                nodelist_chunks = ["\n".join(nodelist[i:i+10]) for i in range(0, len(nodelist), 10)]

            # Process mesh -> Discord
            try:
                embed = meshtodiscord.get_nowait()
                if isinstance(embed, discord.Embed) and public_channel:
                    await public_channel.send(embed=embed)
                meshtodiscord.task_done()
            except queue.Empty:
                pass

            # Process Discord -> mesh
            try:
                msg = discordtomesh.get_nowait()
                if msg.startswith("channel="):
                    idx = int(msg[8:msg.find(" ")])
                    text = msg[msg.find(" ") + 1:]
                    def send_channel(): return self.iface.sendText(text, channelIndex=idx)
                    await asyncio.get_running_loop().run_in_executor(None, send_channel)
                elif msg.startswith("nodenum="):
                    node_id = int(msg[8:msg.find(" ")])
                    text = msg[msg.find(" ") + 1:]
                    def send_node(): return self.iface.sendText(text, destinationId=node_id)
                    await asyncio.get_running_loop().run_in_executor(None, send_node)
                else:
                    def send_all(): return self.iface.sendText(msg, destinationId='^all')
                    await asyncio.get_running_loop().run_in_executor(None, send_all)
                discordtomesh.task_done()
            except queue.Empty:
                pass

            # Process ephemeral node list requests
            try:
                interaction = nodelistq.get_nowait()
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

# ---------- NOAA Alerts Command (Short headline for Mesh) ----------
async def fetch_noaa_alerts(zip_code: str):
    try:
        logging.info(f"Fetching NOAA alerts for ZIP: {zip_code}")
        loc = zipcodes.matching(zip_code)
        if not loc:
            logging.warning(f"No matching location for ZIP: {zip_code}")
            return [f"Invalid ZIP code: {zip_code}"], []

        lat = loc[0]['lat']
        lon = loc[0]['long']
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
        logging.info(f"Requesting NOAA API: {url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.error(f"NOAA API request failed: {resp.status}")
                    return [f"Error fetching NOAA alerts (status {resp.status})"], []
                data = await resp.json()

        discord_alerts = []
        mesh_alerts = []
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

            logging.info(f"Processing alert: Event={event}, Status={status}, Severity={severity}, Urgency={urgency}")

            if status != "Actual":
                logging.info(f"Skipping alert '{event}' (status={status})")
                continue

            if ends:
                try:
                    ends_dt = datetime.fromisoformat(ends).astimezone(timezone.utc)
                    if ends_dt < now:
                        logging.info(f"Skipping alert '{event}' because expired at {ends_dt}")
                        continue
                except Exception as e:
                    logging.warning(f"Failed to parse ends for alert '{event}': {e}")

            # Include if Winter OR high severity/urgency
            if ("winter" in event.lower() or severity in ["Severe", "Extreme"] or urgency in ["Expected", "Immediate"]):
                discord_alerts.append(f"**{event}**: {headline}\n{description}")
                # Short headline for mesh: Event + end time
                end_time_str = ends_dt.strftime('%I:%M %p') if ends else "unknown"
                mesh_alerts.append(f"**{event}** until {end_time_str}")
            else:
                logging.info(f"Skipping alert '{event}' (does not meet criteria)")

        if not discord_alerts:
            discord_alerts = [f"No active/severe or winter weather alerts for ZIP {zip_code}."]
            mesh_alerts = []

        return discord_alerts, mesh_alerts

    except Exception as e:
        logging.error(f"Error fetching NOAA alerts: {e}")
        return [f"Error fetching alerts: {e}"], []

# Slash command
@client.tree.command(name="noaa_alerts", description="Get active/severe NOAA alerts for a ZIP code.")
async def noaa_alerts_command(interaction: discord.Interaction, zip_code: str):
    await interaction.response.defer(ephemeral=False)
    discord_alerts, mesh_alerts = await fetch_noaa_alerts(zip_code)

    embed = discord.Embed(
        title=f"NOAA Alerts for {zip_code}",
        description="\n".join(discord_alerts),
        color=0xff0000
    )
    await interaction.followup.send(embed=embed)

    send_text_chunks("\n".join(mesh_alerts))

# Regular message command
async def handle_noaa_message(message: discord.Message):
    parts = message.content.split()
    if len(parts) != 2:
        await message.channel.send("Usage: `!alerts <ZIP>`")
        return
    zip_code = parts[1]
    discord_alerts, mesh_alerts = await fetch_noaa_alerts(zip_code)

    embed = discord.Embed(
        title=f"NOAA Alerts for {zip_code}",
        description="\n".join(discord_alerts),
        color=0xff0000
    )
    await message.channel.send(embed=embed)

    send_text_chunks("\n".join(mesh_alerts))

@client.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith("!alerts"):
        await handle_noaa_message(message)

# ---------------- Run Bot ----------------
def run_bot():
    try:
        client.run(TOKEN)
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
    finally:
        asyncio.run(client.close())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_bot()
