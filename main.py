import asyncio
import json
import logging
import queue
import sys
import time
from datetime import datetime, timezone
import os
import discord
from discord import app_commands, ButtonStyle, Embed
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

        embed = Embed(
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

# ---------------- Bot Class ----------------
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
            # Connect to Meshtastic interface
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
                # Build active node list
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

# ---------- Standard Commands ----------
@client.tree.command(name="help", description="Shows the help message.")
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    help_text = ("**Command List**\n"
                 "`/sendid` - Send a message to another node.\n"
                 "`/sendnum` - Send a message to another node.\n"
                 "`/active` - Shows all active nodes.\n"
                 "`/help` - Shows this help message.\n")
    for idx, name in CHANNEL_NAMES.items():
        help_text += f"`/{name.lower()}` - Send a message in the {name} channel.\n"
    embed = Embed(title="Meshtastic Bot Help", description=help_text, color=COLOR)
    embed.set_footer(text="Meshtastic Discord Bot by Kavitate")
    embed.set_image(url="https://i.imgur.com/qvo2NkW.jpeg")
    await interaction.followup.send(embed=embed, view=HelpView(), ephemeral=True)

@client.tree.command(name="sendid", description="Send a message to a specific node (hex ID).")
async def sendid(interaction: discord.Interaction, nodeid: str, message: str):
    try:
        if nodeid.startswith("!"):
            nodeid = nodeid[1:]
        nodenum = int(nodeid, 16)
        discordtomesh.put(f"nodenum={nodenum} {message}")
        await interaction.response.send_message(f"Sending to node !{nodeid}: {message}", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid hexadecimal node ID.", ephemeral=True)

@client.tree.command(name="sendnum", description="Send a message to a specific node (decimal ID).")
async def sendnum(interaction: discord.Interaction, nodenum: int, message: str):
    discordtomesh.put(f"nodenum={nodenum} {message}")
    await interaction.response.send_message(f"Sending to node {nodenum}: {message}", ephemeral=True)

# ---------- Dynamic Channel Commands (Public) ----------
def create_channel_command(channel_index: int, channel_name: str):
    @client.tree.command(
        name=channel_name.lower(),
        description=f"Send a message to the {channel_name} channel."
    )
    async def send_channel(interaction: discord.Interaction, message: str):
        discordtomesh.put(f"channel={channel_index} {message}")
        channel = client.get_channel(CHANNEL_ID)
        if channel:
            embed = Embed(
                title=f"Message to {CHANNEL_NAMES[channel_index]}",
                description=message,
                color=COLOR
            )
            embed.set_footer(text=datetime.now().strftime('%d %B %Y %I:%M:%S %p'))
            await channel.send(embed=embed)
        await interaction.response.send_message(f"Message sent to channel {CHANNEL_NAMES[channel_index]}.", ephemeral=True)
    return send_channel

for idx, name in CHANNEL_NAMES.items():
    create_channel_command(idx, name)

# ---------- Active Nodes Command (Ephemeral) ----------
@client.tree.command(name="active", description="Shows active nodes.")
async def active(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    nodelistq.put(interaction)
    await asyncio.sleep(1)
    await interaction.delete_original_response()

# ---------- WXNOW COMMAND ----------------
@client.tree.command(name="wxnow", description="Shows the current weather report from wxnow file.")
async def wxnow_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    if not WX_FILE or not os.path.exists(WX_FILE):
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

        embed = Embed(
            title="Flint Hill Weather Report",
            description="\n".join(report_lines),
            color=COLOR
        )
        embed.set_footer(text=datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p'))
        await interaction.followup.send(embed=embed, ephemeral=False)

        if client.iface and hasattr(client.iface, "myInfo") and client.iface.myInfo:
            def send_lines():
                for line in report_lines:
                    client.iface.sendText(line, destinationId="^all", channelIndex=0)
                    time.sleep(1.5)
            await asyncio.get_running_loop().run_in_executor(None, send_lines)

    except Exception as e:
        await interaction.followup.send(f"Error reading weather file: {e}", ephemeral=False)

# ---------- NOAA ALERTS ----------------
async def fetch_noaa_alerts(zip_code: str):
    try:
        loc = zipcodes.matching(zip_code)
        if not loc:
            return [f"Invalid ZIP code: {zip_code}"]
        lat = loc[0]['lat']
        lon = loc[0]['long']
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return [f"Error fetching NOAA alerts (status {resp.status})"]
                data = await resp.json()

        alerts = []
        now = datetime.now(timezone.utc)

        for feature in data.get("features", []):
            props = feature.get("properties", {})
            status = props.get("status", "")
            event = props.get("event", "")
            headline = props.get("headline", "")
            description = props.get("description", "")
            expires = props.get("expires")
            severity = props.get("severity", "")
            urgency = props.get("urgency", "")

            if status != "Actual":
                continue

            if expires:
                expires_dt = datetime.fromisoformat(expires).astimezone(timezone.utc)
                if expires_dt < now:
                    continue

            if ("winter" in event.lower() or
                "winter" in headline.lower() or
                severity in ["Severe", "Extreme"] or
                urgency in ["Immediate", "Expected"]):
                alerts.append(f"**{event}**: {headline}\n{description}")

        if not alerts:
            alerts = [f"No active/severe or winter weather alerts for ZIP {zip_code}."]
        return alerts

    except Exception as e:
        return [f"Error fetching alerts: {e}"]

# Slash command
@client.tree.command(name="noaa_alerts", description="Get active/severe NOAA alerts for a ZIP code.")
async def noaa_alerts_command(interaction: discord.Interaction, zip_code: str):
    await interaction.response.defer(ephemeral=False)
    alerts_lines = await fetch_noaa_alerts(zip_code)
    embed = Embed(
        title=f"NOAA Alerts for {zip_code}",
        description="\n".join(alerts_lines),
        color=0xff0000
    )
    await interaction.followup.send(embed=embed)

    if client.iface and hasattr(client.iface, "myInfo") and client.iface.myInfo:
        def send_lines():
            for line in alerts_lines:
                client.iface.sendText(line, destinationId="^all", channelIndex=0)
                time.sleep(1.5)
        await asyncio.get_running_loop().run_in_executor(None, send_lines)

# Message command
async def handle_noaa_message(message: discord.Message):
    parts = message.content.split()
    if len(parts) != 2:
        await message.channel.send("Usage: `!alerts <ZIP>`")
        return
    zip_code = parts[1]
    alerts_lines = await fetch_noaa_alerts(zip_code)
    embed = Embed(
        title=f"NOAA Alerts for {zip_code}",
        description="\n".join(alerts_lines),
        color=0xff0000
    )
    await message.channel.send(embed=embed)

    if client.iface and hasattr(client.iface, "myInfo") and client.iface.myInfo:
        def send_lines():
            for line in alerts_lines:
                client.iface.sendText(line, destinationId="^all", channelIndex=0)
                time.sleep(1.5)
        await asyncio.get_running_loop().run_in_executor(None, send_lines)

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
