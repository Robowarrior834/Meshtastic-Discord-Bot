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
            if "disabled_commands" not in config:
                config["disabled_commands"] = []
            return config
    except Exception as e:
        logging.critical(f"Error loading config.json: {e}")
        raise

config = load_config()
DISABLED_COMMANDS = config.get("disabled_commands", [])
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
def command_enabled(name: str) -> bool:
    return name not in DISABLED_COMMANDS

def get_long_name(node_id, nodes):
    try:
        if node_id in nodes:
            return nodes[node_id]['user'].get('longName', 'Unknown')
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
    try:
        if 'decoded' not in packet:
            logging.warning(f"Received packet without 'decoded': {packet}")
            return
        if packet['decoded'].get('portnum') != 'TEXT_MESSAGE_APP':
            return
        channel_index = packet.get('channel', packet['decoded'].get('channel', 0))
        channel_name = CHANNEL_NAMES.get(channel_index, f"Unknown Channel ({channel_index})")
        nodes = interface.nodes if hasattr(interface, 'nodes') else {}
        from_long = get_long_name(packet.get('fromId'), nodes)
        to_long = 'All Nodes' if packet.get('toId') == '^all' else get_long_name(packet.get('toId'), nodes)
        hops_away = '?'
        snr = '?'
        last_heard_str = "Unknown"
        try:
            node_key = packet.get('fromId')
            if node_key in nodes:
                hops_away = nodes[node_key].get('hopsAway', '?')
                snr = nodes[node_key].get('snr', '?')
                lh = nodes[node_key].get('lastHeard', 0)
                if lh:
                    last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
            else:
                try:
                    ik = int(node_key)
                    if ik in nodes:
                        hops_away = nodes[ik].get('hopsAway', '?')
                        snr = nodes[ik].get('snr', '?')
                        lh = nodes[ik].get('lastHeard', 0)
                        if lh:
                            last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
                except Exception:
                    sk = str(node_key)
                    if sk in nodes:
                        hops_away = nodes[sk].get('hopsAway', '?')
                        snr = nodes[sk].get('snr', '?')
                        lh = nodes[sk].get('lastHeard', 0)
                        if lh:
                            last_heard_str = datetime.fromtimestamp(lh, pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p')
        except Exception:
            pass
        logging.info(f"Received message from {from_long} to {to_long}, channel {channel_name}, hopsAway={hops_away}, snr={snr}")
        embed = discord.Embed(
            title=f"📡 Message Received",
            description=packet['decoded'].get('text', ''),
            color=COLOR
        )
        embed.add_field(name="From Node", value=f"{from_long} ({packet.get('fromId')})", inline=True)
        embed.add_field(name="Channel", value=f"{channel_name} (#{channel_index})", inline=True)
        if packet.get('toId') != '^all':
            embed.add_field(name="To Node", value=f"{to_long} ({packet.get('toId')})", inline=True)
        embed.add_field(name="Hops Away", value=str(hops_away), inline=True)
        embed.add_field(name="SNR", value=str(snr), inline=True)
        embed.add_field(name="Last Heard", value=last_heard_str, inline=True)
        embed.set_footer(text=datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p'))
        meshtodiscord.put(embed)
    except Exception as e:
        logging.warning(f"Error in onReceiveMesh: {e}", exc_info=True)

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
                chunk_size = 10
                nodelist_chunks = ["\n".join(nodelist[i:i + chunk_size]) for i in range(0, len(nodelist), chunk_size)]
            try:
                embed = meshtodiscord.get_nowait()
                if isinstance(embed, discord.Embed) and public_channel:
                    await public_channel.send(embed=embed)
                meshtodiscord.task_done()
            except queue.Empty:
                pass
            try:
                msg_data = discordtomesh.get_nowait()
                text = msg_data['text']
                destinationId = msg_data.get('dest', '^all')
                channelIndex = msg_data.get('channel', 0)
                author_name = msg_data.get('author')

                sent_chunks = []
                if self.iface:
                    sent_chunks = send_text_chunks(
                        text,
                        author_name=author_name,
                        destinationId=destinationId,
                        channelIndex=channelIndex
                    )

                if public_channel and sent_chunks:
                    # Get channel name for the embed
                    channel_name = CHANNEL_NAMES.get(channelIndex, f"Unknown Channel ({channelIndex})")
                    
                    # Prepend channel name to each chunk for Discord embed
                    display_chunks = [f"[{channel_name}] {chunk}" for chunk in sent_chunks]

                    embed = discord.Embed(
                        title=f"Meshtastic Message{' by ' + author_name if author_name else ''}",
                        description="\n".join(display_chunks),
                        color=COLOR
                    )
                    embed.set_footer(text=datetime.now(pytz.timezone(TIME_ZONE)).strftime('%d %B %Y %I:%M:%S %p'))
                    await public_channel.send(embed=embed)

                discordtomesh.task_done()
            except queue.Empty:
                pass

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

def queue_meshtastic_message(author_name, text, dest="^all", channel=0):
    discordtomesh.put({'text': text, 'dest': dest, 'channel': channel, 'author': author_name})
    logging.debug(f"Queued message from {author_name} to Meshtastic: {text}")

# ---------------- Discord Commands ----------------

# /sendid
if command_enabled("sendid"):
    @client.tree.command(name="sendid", description="Send a message to a specific node (hex ID).")
    async def sendid(interaction: discord.Interaction, nodeid: str, message: str):
        try:
            if nodeid.startswith("!"):
                nodeid = nodeid[1:]
            nodenum = int(nodeid, 16)
            username = (
                interaction.user.nick
                if interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.nick
                else interaction.user.display_name
            )
            queue_meshtastic_message(username, message, dest=nodenum)
            await interaction.response.send_message(f"Message queued to node !{nodeid}.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid hexadecimal node ID.", ephemeral=True)

# /sendnum
if command_enabled("sendnum"):
    @client.tree.command(name="sendnum", description="Send a message to a specific node (decimal ID).")
    async def sendnum(interaction: discord.Interaction, nodenum: int, message: str):
        username = (
            interaction.user.nick
            if interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.nick
            else interaction.user.display_name
        )
        queue_meshtastic_message(username, message, dest=nodenum)
        await interaction.response.send_message(f"Message queued to node {nodenum}.", ephemeral=True)

# Dynamic channels
def create_channel_command(channel_index: int, channel_name: str):
    if channel_name.lower() in ["longfast", "wxnow", "help", "noaa_alerts", "sendid", "sendnum", "active"]:
        return
    @client.tree.command(
        name=channel_name.lower(),
        description=f"Send a message to the {channel_name} channel."
    )
    async def send_channel(interaction: discord.Interaction, message: str):
        username = (
            interaction.user.nick
            if interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.nick
            else interaction.user.display_name
        )
        queue_meshtastic_message(username, message, channel=channel_index)
        await interaction.response.send_message(f"Message queued to channel {CHANNEL_NAMES[channel_index]}.", ephemeral=True)

for idx, name in CHANNEL_NAMES.items():
    if command_enabled(name.lower()):
        create_channel_command(idx, name)

# /longfast
if command_enabled("longfast"):
    @client.tree.command(name="longfast", description="Send a long fast message to all nodes.")
    async def longfast(interaction: discord.Interaction, message: str):
        username = (
            interaction.user.nick
            if interaction.guild and isinstance(interaction.user, discord.Member) and interaction.user.nick
            else interaction.user.display_name
        )
        channel_index = 0  # longfast is always channel 0
        channel_name = CHANNEL_NAMES.get(channel_index, f"Unknown Channel ({channel_index})")
        
        # Include the channel name in the queued message
        text_with_channel = f"[{channel_name}] {username}: {message}"
        
        # Queue the message
        queue_meshtastic_message(username, message, channel=channel_index)
        
        # Send confirmation in Discord showing channel
        embed = discord.Embed(
            title="Meshtastic Message Queued",
            description=text_with_channel,
            color=COLOR
        )
        embed.set_footer(text=f"{datetime.now().strftime('%d %B %Y %I:%M:%S %p')}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# /wxnow
if command_enabled("wxnow"):
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
                baro_raw = int(data_line[data_line.find('b')+1:data_line.find('b')+6])
                baro = baro_raw / 10.0
                baro_inhg = round(baro * 0.02953, 2)

                report_lines = [
                    f"FH WX {date_line}",
                    f"W {wind_dir}°@{wind_speed}mph G {wind_gust} "
                    f"T {temp}F H {humidity}% P {baro_inhg}inHg"
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

# /active
if command_enabled("active"):
    @client.tree.command(name="active", description="Show currently active nodes.")
    async def active(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        nodelistq.put(interaction)

# /meshdiag
if command_enabled("meshdiag"):
    @client.tree.command(name="meshdiag", description="Show local node diagnostics.")
    async def meshdiag(interaction: discord.Interaction):
        if client.iface:
            nodes = client.iface.nodes
            total = len(nodes)
            heard_15 = sum(1 for n in nodes.values() if time.time() - n.get('lastHeard', 0) <= 900)
            heard_6h = sum(1 for n in nodes.values() if time.time() - n.get('lastHeard', 0) <= 6*3600)
            avg_hops = sum(n.get('hopsAway',0) for n in nodes.values()) / total if total else 0
            worst_hops = max((n.get('hopsAway',0) for n in nodes.values()), default=0)
            avg_snr = sum(n.get('snr',0) for n in nodes.values()) / total if total else 0
            embed = discord.Embed(title="Mesh Diagnostics", color=COLOR)
            embed.add_field(name="Total Nodes", value=str(total))
            embed.add_field(name="Heard ≤15 min", value=str(heard_15))
            embed.add_field(name="Heard ≤6 hrs", value=str(heard_6h))
            embed.add_field(name="Avg Hops", value=f"{avg_hops:.2f}")
            embed.add_field(name="Worst Hops", value=str(worst_hops))
            embed.add_field(name="Avg SNR", value=f"{avg_snr:.2f}")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Meshtastic interface not connected.", ephemeral=True)

# ---------------- Run Bot ----------------
def run_bot():
    try:
        client.run(TOKEN)
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
    finally:
        try:
            asyncio.run(client.close())
        except Exception:
            pass

# ---------------- Logging Setup ----------------
if __name__ == "__main__":
    LOG_DIR = "logs"
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"meshtastic_discord_{datetime.now().strftime('%Y-%m-%d')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("Starting Meshtastic Discord Bot")
    run_bot()
