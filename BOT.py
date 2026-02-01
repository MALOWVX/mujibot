import discord
from discord.ext import commands
from discord.ui import TextInput, Modal, Select
import requests
import json
import os
import random
import io

# --- Configuration ---
# Token from environment variable (set in Railway Variables or .env file)
TOKEN = os.getenv("TOKEN", "YOUR_TOKEN_HERE")
ADMIN_ID = 571430702630043668
DATA_FILE = "user_data.json"

API_URL = "https://danbooru.donmai.us/posts.json"
AUTOCOMPLETE_URL = "https://danbooru.donmai.us/autocomplete.json"
HEADERS = {
    "User-Agent": "DiscordDanbooruBot/4.0",
    "Accept": "application/json"
}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="?", intents=intents)

# --- Bot State ---
history = {} 
video_history = {}  # Separate history for videos
user_data = {}  # {user_id: {"favorites": [...], "view_count": 0}}

# --- Data Management ---
def load_user_data():
    global user_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                user_data = json.load(f)
        except Exception as e:
            print(f"Error loading user data: {e}")
            user_data = {}

def save_user_data():
    """Save user data to database or JSON fallback"""
    global user_data
    
    conn = get_db_connection()
    if not conn:
        # Fallback to JSON
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(user_data, f, indent=2)
        except Exception as e:
            print(f"Error saving user data: {e}")
        return
    
    try:
        cur = conn.cursor()
        for uid, data in user_data.items():
            cur.execute("""
                INSERT INTO users (user_id, view_count, waifame, daily_favs, last_fav_date, favorites)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    view_count = EXCLUDED.view_count,
                    waifame = EXCLUDED.waifame,
                    daily_favs = EXCLUDED.daily_favs,
                    last_fav_date = EXCLUDED.last_fav_date,
                    favorites = EXCLUDED.favorites
            """, (
                uid,
                data.get("view_count", 0),
                data.get("waifame", 0),
                data.get("daily_favs", 0),
                data.get("last_fav_date", ""),
                json.dumps(data.get("favorites", []))
            ))
        conn.commit()
    except Exception as e:
        print(f"Database save error: {e}")
    finally:
        conn.close()

def load_user_data():
    """Load all user data from database"""
    global user_data
    
    conn = get_db_connection()
    if not conn:
        load_user_data_json()
        return
    
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, view_count, waifame, daily_favs, last_fav_date, favorites FROM users")
        rows = cur.fetchall()
        
        for row in rows:
            user_data[row[0]] = {
                "view_count": row[1],
                "waifame": row[2],
                "daily_favs": row[3],
                "last_fav_date": row[4] or "",
                "favorites": json.loads(row[5]) if row[5] else []
            }
        print(f"Loaded {len(rows)} users from database")
    except Exception as e:
        print(f"Database load error: {e}")
        load_user_data_json()
    finally:
        conn.close()

def get_user_data(user_id):
    """Get or create user data"""
    uid = str(user_id)
    if uid not in user_data:
        user_data[uid] = {
            "favorites": [], 
            "view_count": 0,
            "waifame": 0,
            "daily_favs": 0,
            "last_fav_date": ""
        }
    # Ensure new fields exist for old users
    data = user_data[uid]
    if "waifame" not in data: data["waifame"] = 0
    if "daily_favs" not in data: data["daily_favs"] = 0
    if "last_fav_date" not in data: data["last_fav_date"] = ""
    return data

def get_today_date():
    """Get today's date as string"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")

def can_add_favorite(user_id):
    """Check if user can add a favorite today (limit: 5 per day)"""
    data = get_user_data(user_id)
    today = get_today_date()
    
    # Reset if new day
    if data.get("last_fav_date") != today:
        data["daily_favs"] = 0
        data["last_fav_date"] = today
    
    return data["daily_favs"] < 5

def use_daily_favorite(user_id):
    """Use one of the daily favorites slots"""
    data = get_user_data(user_id)
    today = get_today_date()
    
    if data.get("last_fav_date") != today:
        data["daily_favs"] = 0
        data["last_fav_date"] = today
    
    data["daily_favs"] += 1
    save_user_data()
    return 5 - data["daily_favs"]  # Return remaining

def calculate_waifame(post):
    """Calculate waifame earned from viewing an image based on its popularity"""
    score = post.get("score", 0)
    fav_count = post.get("fav_count", 0)
    
    # Base: 1 waifame per view
    # Bonus: based on popularity
    base = 1
    score_bonus = max(0, score) // 50  # +1 per 50 score
    fav_bonus = fav_count // 100  # +1 per 100 favorites
    
    # Artist fame bonus
    artist_bonus = get_artist_fame_bonus(post)
    
    return base + score_bonus + fav_bonus + artist_bonus

def get_artist_fame_bonus(post):
    """Get bonus waifame based on how famous the artist is on Danbooru"""
    artist_tag = post.get("tag_string_artist", "").split()
    
    if not artist_tag:
        return 0
    
    # Get the first (main) artist
    artist_name = artist_tag[0]
    
    # Check cache first (to avoid too many API calls)
    if not hasattr(get_artist_fame_bonus, 'cache'):
        get_artist_fame_bonus.cache = {}
    
    if artist_name in get_artist_fame_bonus.cache:
        post_count = get_artist_fame_bonus.cache[artist_name]
    else:
        # Query Danbooru for artist post count
        try:
            artist_url = f"https://danbooru.donmai.us/tags.json?search[name]={artist_name}"
            resp = requests.get(artist_url, headers=HEADERS, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    post_count = data[0].get("post_count", 0)
                else:
                    post_count = 0
            else:
                post_count = 0
        except Exception as e:
            print(f"Artist lookup error: {e}")
            post_count = 0
        
        # Cache the result
        get_artist_fame_bonus.cache[artist_name] = post_count
    
    # Calculate bonus based on post count
    # More posts = more famous artist = higher bonus
    if post_count >= 10000:
        return 10  # Legendary artist (10k+ posts)
    elif post_count >= 5000:
        return 7   # Very famous (5k-10k)
    elif post_count >= 2000:
        return 5   # Famous (2k-5k)
    elif post_count >= 1000:
        return 3   # Well-known (1k-2k)
    elif post_count >= 500:
        return 2   # Known (500-1k)
    elif post_count >= 100:
        return 1   # Some recognition (100-500)
    else:
        return 0   # New/unknown artist

def add_waifame(user_id, post):
    """Add waifame to user based on image viewed"""
    data = get_user_data(user_id)
    earned = calculate_waifame(post)
    data["waifame"] = data.get("waifame", 0) + earned
    save_user_data()
    return earned, data["waifame"]

def increment_view_count(user_id, post=None):
    """Increment view count for a user and add waifame"""
    data = get_user_data(user_id)
    data["view_count"] = data.get("view_count", 0) + 1
    
    earned = 0
    if post:
        earned = calculate_waifame(post)
        data["waifame"] = data.get("waifame", 0) + earned
    
    save_user_data()
    return data["view_count"], earned, data.get("waifame", 0)

def get_danbooru_image(tags="rating:safe"):
    """Fetch a random image from Danbooru, ensuring it has a valid embeddable URL"""
    try:
        params = {"tags": tags, "random": "true", "limit": 10}
        resp = requests.get(API_URL, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                for post in data:
                    file_url = post.get('file_url') or post.get('large_file_url')
                    if file_url:
                        if file_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                            post['file_url'] = file_url
                            return post
                return None
    except Exception as e:
        print(f"API Error: {e}")
    return None

def get_tag_suggestions(query):
    """Get tag suggestions from Danbooru autocomplete API"""
    try:
        params = {"search[query]": query, "search[type]": "tag_query", "limit": 10}
        resp = requests.get(AUTOCOMPLETE_URL, headers=HEADERS, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return [item.get("value", item.get("label", "")) for item in data[:10]]
    except Exception as e:
        print(f"Autocomplete Error: {e}")
    return []

def get_danbooru_video(tags="rating:safe"):
    """Fetch a random video from Danbooru (.mp4 or .webm only)"""
    try:
        # Add 'video' tag to ensure we get videos
        video_tags = f"{tags} video"
        params = {"tags": video_tags, "random": "true", "limit": 20}
        resp = requests.get(API_URL, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                for post in data:
                    file_ext = post.get('file_ext', '')
                    file_url = post.get('file_url') or post.get('large_file_url')
                    if file_url and file_ext in ['mp4', 'webm']:
                        post['file_url'] = file_url
                        return post
                return None
    except Exception as e:
        print(f"Video API Error: {e}")
    return None

@bot.event
async def on_ready():
    print(f'Connecté en tant que {bot.user}')
    load_user_data()

# --- HELPER FUNCTION TO SEND MAIN VIEW ---
async def send_main_view(ctx, post, tags, user_id):
    """Handles fetching and sending the View to prevent double windows"""
    
    file_url = post.get('file_url')
    post_id = post.get('id')
    post_url = f"https://danbooru.donmai.us/posts/{post_id}"
    
    if ctx.guild.id not in history:
        history[ctx.guild.id] = []
    history[ctx.guild.id].append(post)
    
    # Increment view count and earn waifame
    view_count, earned, total_waifame = increment_view_count(user_id, post)
    
    # Create View
    view = ImageView(ctx.guild.id, post, tags, user_id)
    
    embed = discord.Embed(title=f"Danbooru #{post_id}", url=post_url, color=0xBB86FC)
    embed.set_image(url=file_url)
    embed.set_footer(text=f"👁️ {view_count} vues | 💰 +{earned} Waifame ({total_waifame} total)")
    
    await ctx.send(embed=embed, view=view)

@bot.command()
async def next(ctx, *, tags: str = "rating:safe"):
    """Récupère une image aléatoire"""
    
    # Log user info to terminal
    print(f"[?next] Utilisateur: {ctx.author.name} (ID: {ctx.author.id}) | Tags: {tags}")

    post = get_danbooru_image(tags)
    
    if post:
        await send_main_view(ctx, post, tags, ctx.author.id)
    else:
        await ctx.send("Impossible de trouver une image avec ces tags.")

@bot.command()
async def vnext(ctx, *, tags: str = "rating:safe"):
    """Récupère une vidéo aléatoire"""
    
    # Log user info to terminal
    print(f"[?vnext] Utilisateur: {ctx.author.name} (ID: {ctx.author.id}) | Tags: {tags}")

    await ctx.send("🔄 Chargement de la vidéo...", delete_after=3)
    
    post = get_danbooru_video(tags)
    
    if post:
        file_url = post.get('file_url')
        post_id = post.get('id')
        post_url = f"https://danbooru.donmai.us/posts/{post_id}"
        file_ext = post.get('file_ext', 'mp4')
        
        # Add to video history
        if ctx.guild.id not in video_history:
            video_history[ctx.guild.id] = []
        video_history[ctx.guild.id].append(post)
        
        # Earn waifame
        view_count, earned, total_waifame = increment_view_count(ctx.author.id, post)
        
        # Try to download and upload video as attachment
        video_msg = None
        try:
            resp = requests.get(file_url, headers=HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.content) < 8_000_000:  # 8MB limit
                video_file = discord.File(io.BytesIO(resp.content), filename=f"video_{post_id}.{file_ext}")
                video_msg = await ctx.send(file=video_file)
            else:
                # Too large, send as link
                video_msg = await ctx.send(f"📹 Vidéo trop volumineuse, clic ici: {file_url}")
        except Exception as e:
            print(f"Video download error: {e}")
            video_msg = await ctx.send(f"📹 {file_url}")
        
        # Create embed with info
        embed = discord.Embed(title=f"🎬 Vidéo #{post_id}", url=post_url, color=0x9B59B6)
        embed.add_field(name="👁️ Vues", value=str(view_count), inline=True)
        embed.add_field(name="💰 Waifame", value=f"+{earned} ({total_waifame} total)", inline=True)
        embed.set_footer(text=f"Tags: {post.get('tag_string', '')[:50]}...")
        
        # Create view and send embed with buttons
        view = VideoView(ctx.guild.id, post, tags, ctx.author.id, video_msg)
        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send("Impossible de trouver une vidéo avec ces tags. Essaie d'autres tags !")

@bot.command()
async def favorites_list(ctx):
    """Affiche tes images favorites (privé - visible uniquement par toi)"""
    user_favs = get_user_data(ctx.author.id).get("favorites", [])
    
    if len(user_favs) == 0:
        await ctx.send("Tu n'as pas encore de favoris. Ajoutes-en en cliquant sur le bouton ❤️ !", ephemeral=True, delete_after=10)
        return

    view = FavoritesView(ctx.author.id)
    first_post = user_favs[0]
    
    file_url = first_post.get('file_url')
    post_id = first_post.get('id')
    
    embed = discord.Embed(title=f"❤️ Favori #{post_id}", url=f"https://danbooru.donmai.us/posts/{post_id}", color=0xFF0055)
    embed.set_image(url=file_url)
    embed.set_footer(text=f"1/{len(user_favs)} | Visible uniquement par toi")
    
    # Send as ephemeral (only visible to command user)
    await ctx.author.send(embed=embed, view=view)
    await ctx.send("📬 Regarde tes MP pour ta liste de favoris !", delete_after=5)

@bot.command()
async def stats(ctx):
    """Affiche tes statistiques"""
    data = get_user_data(ctx.author.id)
    view_count = data.get("view_count", 0)
    fav_count = len(data.get("favorites", []))
    waifame = data.get("waifame", 0)
    
    # Check daily favorites
    today = get_today_date()
    if data.get("last_fav_date") != today:
        daily_remaining = 5
    else:
        daily_remaining = 5 - data.get("daily_favs", 0)
    
    embed = discord.Embed(title="📊 Tes Statistiques", color=0x00FF88)
    embed.add_field(name="👁️ Images Vues", value=str(view_count), inline=True)
    embed.add_field(name="❤️ Favoris", value=str(fav_count), inline=True)
    embed.add_field(name="💰 Waifame", value=str(waifame), inline=True)
    embed.add_field(name="⭐ Favoris Restants", value=f"{daily_remaining}/5 aujourd'hui", inline=True)
    
    await ctx.send(embed=embed)

@bot.command()
async def leaderboard(ctx):
    """Affiche le classement Waifame du serveur"""
    
    # Get all users and their waifame
    leaderboard_data = []
    for uid, data in user_data.items():
        waifame = data.get("waifame", 0)
        if waifame > 0:  # Only include users with waifame
            leaderboard_data.append((uid, waifame))
    
    # Sort by waifame (descending)
    leaderboard_data.sort(key=lambda x: x[1], reverse=True)
    
    if not leaderboard_data:
        await ctx.send("Personne n'a encore de Waifame ! Utilise `?next` pour commencer à en gagner.")
        return
    
    # Build leaderboard embed
    embed = discord.Embed(title="🏆 Classement Waifame", color=0xFFD700)
    
    medals = ["🥇", "🥈", "🥉"]
    leaderboard_text = ""
    
    for i, (uid, waifame) in enumerate(leaderboard_data[:10]):  # Top 10
        # Try to get username
        try:
            user = await bot.fetch_user(int(uid))
            username = user.name
        except:
            username = f"Utilisateur #{uid[:8]}"
        
        # Add medal for top 3
        if i < 3:
            rank = medals[i]
        else:
            rank = f"**{i+1}.**"
        
        leaderboard_text += f"{rank} {username} — **{waifame}** 💰\n"
    
    embed.description = leaderboard_text
    embed.set_footer(text=f"Total: {len(leaderboard_data)} participants")
    
    await ctx.send(embed=embed)

@bot.command()
async def logs(ctx, user_id: int = None):
    """[ADMIN] Affiche les informations collectées sur un utilisateur"""
    # Admin only
    if ctx.author.id != ADMIN_ID:
        await ctx.send("Désolé, seul mon maître peut utiliser cette commande.")
        return
    
    if user_id is None:
        await ctx.send("❌ Usage: `?logs <user_id>`")
        return
    
    uid = str(user_id)
    
    # Check if user exists in data
    if uid not in user_data:
        await ctx.send(f"❌ Aucune donnée trouvée pour l'utilisateur `{user_id}`.\nCet utilisateur n'a jamais utilisé le bot.")
        return
    
    data = user_data[uid]
    view_count = data.get("view_count", 0)
    favorites = data.get("favorites", [])
    fav_count = len(favorites)
    waifame = data.get("waifame", 0)
    daily_favs = data.get("daily_favs", 0)
    last_fav_date = data.get("last_fav_date", "Jamais")
    
    # Try to get user info from Discord
    username = "Utilisateur inconnu"
    avatar_url = None
    account_created = "Inconnu"
    
    try:
        user = await bot.fetch_user(user_id)
        username = f"{user.name}#{user.discriminator}" if user.discriminator != "0" else user.name
        avatar_url = user.avatar.url if user.avatar else None
        account_created = user.created_at.strftime("%d/%m/%Y %H:%M")
    except:
        pass
    
    # Try to get member info (for status, device, etc.)
    member = None
    status_emoji = "⚫"
    status_text = "Inconnu"
    device_info = "Inconnu"
    activity_text = "Aucune"
    join_date = "Inconnu"
    roles_text = "Aucun"
    
    try:
        member = ctx.guild.get_member(user_id)
        if member:
            # Status
            status_map = {
                discord.Status.online: ("🟢", "En ligne"),
                discord.Status.idle: ("🟡", "Absent"),
                discord.Status.dnd: ("🔴", "Ne pas déranger"),
                discord.Status.offline: ("⚫", "Hors ligne")
            }
            status_emoji, status_text = status_map.get(member.status, ("⚫", "Inconnu"))
            
            # Device detection
            devices = []
            if member.desktop_status != discord.Status.offline:
                devices.append("💻 Desktop")
            if member.mobile_status != discord.Status.offline:
                devices.append("📱 Mobile")
            if member.web_status != discord.Status.offline:
                devices.append("🌐 Web")
            device_info = ", ".join(devices) if devices else "Hors ligne"
            
            # Activity
            if member.activities:
                for activity in member.activities:
                    if isinstance(activity, discord.Game):
                        activity_text = f"🎮 Joue à {activity.name}"
                        break
                    elif isinstance(activity, discord.Streaming):
                        activity_text = f"📺 Stream: {activity.name}"
                        break
                    elif isinstance(activity, discord.Spotify):
                        activity_text = f"🎵 Spotify: {activity.title}"
                        break
                    elif isinstance(activity, discord.CustomActivity):
                        if activity.name:
                            activity_text = f"💬 {activity.name}"
                        break
            
            # Join date
            if member.joined_at:
                join_date = member.joined_at.strftime("%d/%m/%Y %H:%M")
            
            # Roles (top 5)
            roles = [r.name for r in member.roles if r.name != "@everyone"][:5]
            if roles:
                roles_text = ", ".join(roles)
                if len(member.roles) > 6:
                    roles_text += f" (+{len(member.roles) - 6})"
    except:
        pass
    
    embed = discord.Embed(title=f"🔍 Logs - {username}", color=0xFF6600)
    
    # Section 1: Discord Info
    embed.add_field(name="🆔 ID Utilisateur", value=str(user_id), inline=True)
    embed.add_field(name=f"{status_emoji} Statut", value=status_text, inline=True)
    embed.add_field(name="� Appareil", value=device_info, inline=True)
    embed.add_field(name="🎮 Activité", value=activity_text, inline=True)
    embed.add_field(name="📅 Compte créé", value=account_created, inline=True)
    embed.add_field(name="📥 A rejoint le", value=join_date, inline=True)
    
    # Section 2: Bot Stats
    embed.add_field(name="───── Stats Bot ─────", value="\u200b", inline=False)
    embed.add_field(name="�👁️ Images Vues", value=str(view_count), inline=True)
    embed.add_field(name="❤️ Favoris", value=str(fav_count), inline=True)
    embed.add_field(name="💰 Waifame", value=str(waifame), inline=True)
    embed.add_field(name="⭐ Favoris Aujourd'hui", value=f"{daily_favs}/5", inline=True)
    embed.add_field(name="📅 Dernier Favori", value=last_fav_date, inline=True)
    embed.add_field(name="🎭 Rôles", value=roles_text, inline=True)
    
    if favorites:
        # Show first 10 favorite IDs
        fav_ids = [str(f.get("id", "?")) for f in favorites[:10]]
        fav_list = ", ".join(fav_ids)
        if fav_count > 10:
            fav_list += f" ... (+{fav_count - 10} autres)"
        embed.add_field(name="📋 IDs des Favoris", value=fav_list, inline=False)
    
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    
    embed.set_footer(text=f"Demandé par {ctx.author.name}")
    
    await ctx.send(embed=embed)

@bot.command()
async def quiz(ctx):
    """Lance un quiz d'image - devine le personnage !"""
    
    # Log user info to terminal
    print(f"[?quiz] Utilisateur: {ctx.author.name} (ID: {ctx.author.id})")
    
    # Fetch a random image with character tags
    post = get_danbooru_image("rating:safe 1girl")
    
    if not post:
        await ctx.send("Impossible de trouver une image pour le quiz. Réessaie !")
        return
    
    # Get character tags
    char_tags = post.get("tag_string_character", "").split()
    if not char_tags:
        # Try another search with different tags
        post = get_danbooru_image("rating:safe solo")
        if post:
            char_tags = post.get("tag_string_character", "").split()
    
    if not char_tags:
        await ctx.send("Impossible de trouver une image avec des tags de personnage. Réessaie !")
        return
    
    correct_answer = char_tags[0].replace("_", " ").title()
    
    # Large list of popular anime character names as decoys
    all_decoys = [
        "Hatsune Miku", "Sakura Haruno", "Rem", "Emilia", "Zero Two", "Asuna Yuuki",
        "Mikasa Ackerman", "Hinata Hyuga", "Naruto Uzumaki", "Sasuke Uchiha",
        "Goku", "Vegeta", "Luffy", "Zoro", "Nami", "Robin", "Erza Scarlet",
        "Lucy Heartfilia", "Natsu Dragneel", "Megumin", "Aqua", "Darkness",
        "Tohru", "Kanna Kamui", "Saber", "Rin Tohsaka", "Shinobu Oshino",
        "Taiga Aisaka", "Misaka Mikoto", "Kurisu Makise", "Mai Sakurajima",
        "Nezuko Kamado", "Tanjiro Kamado", "Zenitsu Agatsuma", "Inosuke Hashibira",
        "Yor Forger", "Anya Forger", "Power", "Makima", "Denji", "Aki Hayakawa",
        "Marin Kitagawa", "Chika Fujiwara", "Kaguya Shinomiya", "Ai Hoshino",
        "Frieren", "Fern", "Bocchi", "Ryo Yamada", "Kobayashi", "Elma",
        "Yuki Nagato", "Haruhi Suzumiya", "C.C.", "Lelouch", "Levi Ackerman",
        "Eren Yeager", "Historia Reiss", "Annie Leonhart", "Violet Evergarden",
        "Raphtalia", "Naofumi", "Aqua Hoshino", "Ruby Hoshino", "Kana Arima"
    ]
    
    # Filter out the correct answer and pick 3 random decoys
    available_decoys = [d for d in all_decoys if d.lower() != correct_answer.lower()]
    wrong_answers = random.sample(available_decoys, min(3, len(available_decoys)))
    
    # Shuffle answers
    all_answers = [correct_answer] + wrong_answers
    random.shuffle(all_answers)
    
    file_url = post.get('file_url')
    post_id = post.get('id')
    
    embed = discord.Embed(title="🎮 Quiz Personnage !", description="Qui est ce personnage ?", color=0xFFD700)
    embed.set_image(url=file_url)
    embed.set_footer(text="Sélectionne la bonne réponse ci-dessous !")
    
    view = QuizView(correct_answer, all_answers, post_id, ctx.author.id)
    await ctx.send(embed=embed, view=view)

# --- Tag Suggestion Modal ---
class TagSearchModal(discord.ui.Modal):
    def __init__(self, original_message, user_id):
        super().__init__(title="Rechercher sur Danbooru")
        self.original_message = original_message
        self.user_id = user_id
        self.add_item(TextInput(label="Entre des tags", placeholder="ex: chat, yeux_bleus", required=True))

    async def on_submit(self, interaction: discord.Interaction):
        query = self.children[0].value
        
        # Get tag suggestions
        suggestions = get_tag_suggestions(query)
        
        if suggestions and len(suggestions) > 1:
            # Show tag selector
            await interaction.response.send_message(
                "🏷️ **Suggestions de tags** - Sélectionne un tag ou recherche avec ta requête originale :",
                view=TagSelectView(self.original_message, suggestions, query, self.user_id),
                ephemeral=True
            )
        else:
            # No suggestions, search directly
            await interaction.response.defer(thinking=False)
            await self.do_search(interaction, query)
    
    async def do_search(self, interaction, tags):
        post = get_danbooru_image(tags)
        
        if post:
            if interaction.guild.id not in history:
                history[interaction.guild.id] = []
            history[interaction.guild.id].append(post)
            
            increment_view_count(self.user_id)
            
            file_url = post.get('file_url')
            post_id = post.get('id')
            post_url = f"https://danbooru.donmai.us/posts/{post_id}"
            
            new_view = ImageView(interaction.guild.id, post, tags, self.user_id)
            
            embed = discord.Embed(title=f"🔍 Recherche: {tags}", url=post_url, color=0x00FFFF)
            embed.set_image(url=file_url)
            embed.set_footer(text=f"ID: {post_id}")
            
            await self.original_message.edit(embed=embed, view=new_view)
        else:
            await interaction.followup.send(f"Impossible de trouver des images avec les tags: {tags}", ephemeral=True)

class TagSelectView(discord.ui.View):
    """Dropdown for tag suggestions"""
    def __init__(self, original_message, suggestions, original_query, user_id):
        super().__init__(timeout=60)
        self.original_message = original_message
        self.original_query = original_query
        self.user_id = user_id
        
        options = [discord.SelectOption(label=tag[:100], value=tag[:100]) for tag in suggestions[:10]]
        options.append(discord.SelectOption(label=f"🔍 Utiliser: {original_query[:50]}", value=original_query))
        
        self.select = Select(placeholder="Sélectionne un tag...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)
    
    async def select_callback(self, interaction: discord.Interaction):
        selected_tag = self.select.values[0]
        await interaction.response.defer(thinking=False)
        
        post = get_danbooru_image(selected_tag)
        
        if post:
            if interaction.guild.id not in history:
                history[interaction.guild.id] = []
            history[interaction.guild.id].append(post)
            
            increment_view_count(self.user_id)
            
            file_url = post.get('file_url')
            post_id = post.get('id')
            post_url = f"https://danbooru.donmai.us/posts/{post_id}"
            
            new_view = ImageView(interaction.guild.id, post, selected_tag, self.user_id)
            
            embed = discord.Embed(title=f"🔍 Recherche: {selected_tag}", url=post_url, color=0x00FFFF)
            embed.set_image(url=file_url)
            embed.set_footer(text=f"ID: {post_id}")
            
            await self.original_message.edit(embed=embed, view=new_view)
            await interaction.delete_original_response()
        else:
            await interaction.followup.send(f"Impossible de trouver des images avec le tag: {selected_tag}", ephemeral=True)

class QuizView(discord.ui.View):
    """Quiz game view"""
    def __init__(self, correct_answer, all_answers, post_id, user_id):
        super().__init__(timeout=30)
        self.correct_answer = correct_answer
        self.post_id = post_id
        self.user_id = user_id
        self.answered = False
        
        for i, answer in enumerate(all_answers):
            btn = discord.ui.Button(label=answer[:80], style=discord.ButtonStyle.primary, row=0)
            btn.callback = self.make_callback(answer)
            self.add_item(btn)
    
    def make_callback(self, answer):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.send_message("Tu as déjà répondu au quiz !", ephemeral=True)
                return
            
            self.answered = True
            is_correct = answer.lower() == self.correct_answer.lower()
            
            # Disable all buttons
            for child in self.children:
                child.disabled = True
                if child.label.lower() == self.correct_answer.lower():
                    child.style = discord.ButtonStyle.green
                elif child.label == answer and not is_correct:
                    child.style = discord.ButtonStyle.red
            
            if is_correct:
                result = "✅ **Correct !** Bien joué !"
            else:
                result = f"❌ **Faux !** La réponse était: **{self.correct_answer}**"
            
            embed = interaction.message.embeds[0]
            embed.description = result
            embed.color = 0x00FF00 if is_correct else 0xFF0000
            
            await interaction.response.edit_message(embed=embed, view=self)
        
        return callback

class ImageView(discord.ui.View):
    """Main View with Navigation + Rating Buttons + HEART + SEARCH + DOWNLOAD"""
    def __init__(self, guild_id, current_post, tags="rating:safe", user_id=None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.post = current_post
        self.tags = tags
        self.user_id = user_id
        
        # 1. Rating Buttons
        self.safe_btn = discord.ui.Button(label="Safe", style=discord.ButtonStyle.green, row=0)
        self.safe_btn.callback = self.safe_callback
        self.add_item(self.safe_btn)

        self.ques_btn = discord.ui.Button(label="Douteux", style=discord.ButtonStyle.gray, row=0)
        self.ques_btn.callback = self.ques_callback
        self.add_item(self.ques_btn)

        self.expl_btn = discord.ui.Button(label="Explicite", style=discord.ButtonStyle.gray, row=0)
        self.expl_btn.callback = self.expl_callback
        self.add_item(self.expl_btn)

        # 2. Navigation Buttons
        self.next_btn = discord.ui.Button(label="Suivant", style=discord.ButtonStyle.blurple, row=1)
        self.next_btn.callback = self.next_callback
        self.add_item(self.next_btn)

        self.rewind_btn = discord.ui.Button(label="Précédent", style=discord.ButtonStyle.gray, row=1)
        self.rewind_btn.callback = self.rewind_callback
        self.add_item(self.rewind_btn)

        # 3. SEARCH BUTTON
        self.search_btn = discord.ui.Button(label="🔍 Rechercher", style=discord.ButtonStyle.primary, row=1)
        self.search_btn.callback = self.search_callback
        self.add_item(self.search_btn)

        # 4. FAVORITE BUTTON
        is_fav = False
        if user_id:
            user_favs = get_user_data(user_id).get("favorites", [])
            is_fav = any(p.get('id') == self.post.get('id') for p in user_favs)
        
        fav_style = discord.ButtonStyle.green if is_fav else discord.ButtonStyle.gray
        fav_label = "💔" if is_fav else "❤️"
        
        self.fav_btn = discord.ui.Button(label=fav_label, style=fav_style, row=1)
        self.fav_btn.callback = self.fav_callback
        self.add_item(self.fav_btn)

        # 5. DOWNLOAD BUTTON (Link button - opens URL directly)
        file_url = current_post.get('file_url', '')
        if file_url:
            self.download_btn = discord.ui.Button(label="📥", style=discord.ButtonStyle.link, url=file_url, row=0)
            self.add_item(self.download_btn)

        # 6. HELP BUTTON
        self.help_btn = discord.ui.Button(label="❓", style=discord.ButtonStyle.secondary, row=2)
        self.help_btn.callback = self.help_callback
        self.add_item(self.help_btn)

        self.current_tags = tags
        self.update_button_colors()

    async def check_user(self, interaction: discord.Interaction) -> bool:
        """Check if the user clicking is the original command user"""
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Seule la personne qui a lancé cette commande peut utiliser ces boutons.", 
                ephemeral=True
            )
            return False
        return True

    def update_button_colors(self):
        styles = [discord.ButtonStyle.gray, discord.ButtonStyle.gray, discord.ButtonStyle.gray]
        if "safe" in self.current_tags: styles[0] = discord.ButtonStyle.green
        if "questionable" in self.current_tags: styles[1] = discord.ButtonStyle.blurple
        if "explicit" in self.current_tags: styles[2] = discord.ButtonStyle.red

        self.safe_btn.style = styles[0]
        self.ques_btn.style = styles[1]
        self.expl_btn.style = styles[2]

    async def safe_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        self.current_tags = "rating:safe"
        self.update_button_colors()
        await self.update_image(interaction)

    async def ques_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        self.current_tags = "rating:questionable"
        self.update_button_colors()
        await self.update_image(interaction)

    async def expl_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        self.current_tags = "rating:explicit"
        self.update_button_colors()
        await self.update_image(interaction)

    async def search_callback(self, interaction: discord.Interaction):
        """Opens Search Modal with tag suggestions"""
        if not await self.check_user(interaction): return
        await interaction.response.send_modal(TagSearchModal(interaction.message, self.user_id))

    async def help_callback(self, interaction: discord.Interaction):
        """Show command list"""
        help_text = """**📜 Commandes:**
`?next [tags]` - Récupère une image aléatoire
`?favorites_list` - Affiche tes favoris (MP)
`?stats` - Affiche tes statistiques
`?quiz` - Jeu de devinette de personnage

**🔘 Boutons:**
• **Safe/Douteux/Explicite** - Filtrer par classification
• **Suivant** - Image suivante
• **Précédent** - Revenir en arrière
• **🔍 Rechercher** - Rechercher avec des tags
• **❤️** - Ajouter/retirer des favoris
• **📥** - Télécharger l'image"""
        
        await interaction.response.send_message(help_text, ephemeral=True)

    async def next_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        await self.update_image(interaction)

    async def update_image(self, interaction: discord.Interaction):
        """Helper to fetch and show new image"""
        await interaction.response.defer(thinking=False)
        
        post = get_danbooru_image(self.current_tags)
        
        if post:
            if self.guild_id not in history:
                history[self.guild_id] = []
            history[self.guild_id].append(post)
            
            self.post = post
            
            # Increment view count and earn waifame
            if self.user_id:
                view_count, earned, total_waifame = increment_view_count(self.user_id, post)
            else:
                view_count, earned, total_waifame = 0, 0, 0
            
            # Update download button URL
            file_url = post.get('file_url')
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "📥":
                    self.remove_item(child)
                    break
            if file_url:
                self.download_btn = discord.ui.Button(label="📥", style=discord.ButtonStyle.link, url=file_url, row=0)
                self.add_item(self.download_btn)

            # Update favorite button state for new image
            if self.user_id:
                user_favs = get_user_data(self.user_id).get("favorites", [])
                is_fav = any(p.get('id') == post.get('id') for p in user_favs)
                self.fav_btn.label = "💔" if is_fav else "❤️"
                self.fav_btn.style = discord.ButtonStyle.green if is_fav else discord.ButtonStyle.gray

            post_id = post.get('id')

            embed = discord.Embed(title=f"Danbooru #{post_id}", url=f"https://danbooru.donmai.us/posts/{post_id}", color=0xBB86FC)
            embed.set_image(url=file_url)
            embed.set_footer(text=f"👁️ {view_count} vues | 💰 +{earned} Waifame ({total_waifame} total)")
            
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.followup.send("Erreur lors de la récupération de l'image.", ephemeral=True)

    async def rewind_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        if self.guild_id in history and len(history[self.guild_id]) > 1:
            history[self.guild_id].pop()
            prev_post = history[self.guild_id][-1]
            self.post = prev_post
            
            file_url = prev_post.get('file_url')
            post_id = prev_post.get('id')

            # Update favorite button state for this image
            if self.user_id:
                user_favs = get_user_data(self.user_id).get("favorites", [])
                is_fav = any(p.get('id') == post_id for p in user_favs)
                self.fav_btn.label = "💔" if is_fav else "❤️"
                self.fav_btn.style = discord.ButtonStyle.green if is_fav else discord.ButtonStyle.gray

            embed = discord.Embed(title=f"Danbooru #{post_id}", url=f"https://danbooru.donmai.us/posts/{post_id}", color=0xBB86FC)
            embed.set_image(url=file_url)
            embed.set_footer(text=f"Tags: {prev_post.get('tag_string', '')[:100]}...")
            
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("Rien à revenir.", ephemeral=True)

    async def fav_callback(self, interaction: discord.Interaction):
        """Add or Remove from user's favorites list"""
        if not await self.check_user(interaction): return
        if not self.user_id:
            self.user_id = interaction.user.id
        
        user_favs = get_user_data(self.user_id).get("favorites", [])
        pid = self.post.get('id')
        
        # Check if already favorited
        is_fav = any(p.get('id') == pid for p in user_favs)
        
        if is_fav:
            # Remove from favorites (no limit for removing)
            user_data[str(self.user_id)]["favorites"] = [p for p in user_favs if p.get('id') != pid]
            self.fav_btn.label = "❤️"
            self.fav_btn.style = discord.ButtonStyle.gray
            save_user_data()
            await interaction.response.send_message("💔 Retiré de tes favoris.", ephemeral=True)
        else:
            # Check daily limit before adding
            if not can_add_favorite(self.user_id):
                await interaction.response.send_message(
                    "❌ Tu as atteint ta limite de **5 favoris par jour** !\nReviens demain pour en ajouter d'autres. 💫", 
                    ephemeral=True
                )
                return
            
            # Add to favorites
            fav_post = {
                "id": self.post.get("id"),
                "file_url": self.post.get("file_url"),
                "rating": self.post.get("rating"),
                "tag_string": self.post.get("tag_string"),
                "tag_string_character": self.post.get("tag_string_character", "")
            }
            get_user_data(self.user_id)["favorites"].append(fav_post)
            remaining = use_daily_favorite(self.user_id)
            self.fav_btn.label = "💔"
            self.fav_btn.style = discord.ButtonStyle.green
            save_user_data()
            await interaction.response.send_message(f"❤️ Ajouté à tes favoris ! ({remaining}/5 restants aujourd'hui)", ephemeral=True)
        
        await interaction.message.edit(view=self)

class VideoView(discord.ui.View):
    """View for video navigation with rating buttons"""
    def __init__(self, guild_id, current_post, tags="rating:safe", user_id=None, video_message=None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.post = current_post
        self.tags = tags
        self.user_id = user_id
        self.video_message = video_message  # Reference to the video message
        
        # 1. Rating Buttons
        self.safe_btn = discord.ui.Button(label="Safe", style=discord.ButtonStyle.green, row=0)
        self.safe_btn.callback = self.safe_callback
        self.add_item(self.safe_btn)

        self.ques_btn = discord.ui.Button(label="Douteux", style=discord.ButtonStyle.gray, row=0)
        self.ques_btn.callback = self.ques_callback
        self.add_item(self.ques_btn)

        self.expl_btn = discord.ui.Button(label="Explicite", style=discord.ButtonStyle.gray, row=0)
        self.expl_btn.callback = self.expl_callback
        self.add_item(self.expl_btn)

        # 2. Navigation Buttons
        self.next_btn = discord.ui.Button(label="Suivant 🎬", style=discord.ButtonStyle.blurple, row=1)
        self.next_btn.callback = self.next_callback
        self.add_item(self.next_btn)

        self.rewind_btn = discord.ui.Button(label="Précédent", style=discord.ButtonStyle.gray, row=1)
        self.rewind_btn.callback = self.rewind_callback
        self.add_item(self.rewind_btn)

        # 3. Download Button (link to video)
        file_url = current_post.get('file_url', '')
        if file_url:
            self.download_btn = discord.ui.Button(label="📥", style=discord.ButtonStyle.link, url=file_url, row=0)
            self.add_item(self.download_btn)

        # 4. Help Button
        self.help_btn = discord.ui.Button(label="❓", style=discord.ButtonStyle.secondary, row=1)
        self.help_btn.callback = self.help_callback
        self.add_item(self.help_btn)

        self.current_tags = tags
        self.update_button_colors()

    async def check_user(self, interaction: discord.Interaction) -> bool:
        """Check if the user clicking is the original command user"""
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Seule la personne qui a lancé cette commande peut utiliser ces boutons.", 
                ephemeral=True
            )
            return False
        return True

    def update_button_colors(self):
        styles = [discord.ButtonStyle.gray, discord.ButtonStyle.gray, discord.ButtonStyle.gray]
        if "safe" in self.current_tags: styles[0] = discord.ButtonStyle.green
        if "questionable" in self.current_tags: styles[1] = discord.ButtonStyle.blurple
        if "explicit" in self.current_tags: styles[2] = discord.ButtonStyle.red

        self.safe_btn.style = styles[0]
        self.ques_btn.style = styles[1]
        self.expl_btn.style = styles[2]

    async def safe_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        await interaction.response.defer()
        self.current_tags = "rating:safe"
        self.update_button_colors()
        await self.update_video(interaction, deferred=True)

    async def ques_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        await interaction.response.defer()
        self.current_tags = "rating:questionable"
        self.update_button_colors()
        await self.update_video(interaction, deferred=True)

    async def expl_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        await interaction.response.defer()
        self.current_tags = "rating:explicit"
        self.update_button_colors()
        await self.update_video(interaction, deferred=True)

    async def help_callback(self, interaction: discord.Interaction):
        """Show command list for videos"""
        help_text = """**🎬 Commandes Vidéo:**
`?vnext [tags]` - Récupère une vidéo aléatoire

**🔘 Boutons:**
• **Safe/Douteux/Explicite** - Filtrer par classification
• **Suivant 🎬** - Vidéo suivante
• **Précédent** - Revenir en arrière
• **📥** - Télécharger la vidéo"""
        
        await interaction.response.send_message(help_text, ephemeral=True)

    async def next_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        await interaction.response.defer()
        await self.update_video(interaction, deferred=True)

    async def update_video(self, interaction: discord.Interaction, deferred=False):
        """Fetch and show new video"""
        if not deferred:
            await interaction.response.defer()
        
        post = get_danbooru_video(self.current_tags)
        
        if post:
            if self.guild_id not in video_history:
                video_history[self.guild_id] = []
            video_history[self.guild_id].append(post)
            
            self.post = post
            
            # Increment view count and earn waifame
            if self.user_id:
                view_count, earned, total_waifame = increment_view_count(self.user_id, post)
            else:
                view_count, earned, total_waifame = 0, 0, 0
            
            file_url = post.get('file_url')
            post_id = post.get('id')
            post_url = f"https://danbooru.donmai.us/posts/{post_id}"
            file_ext = post.get('file_ext', 'mp4')
            
            # Update download button URL
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "📥":
                    self.remove_item(child)
                    break
            if file_url:
                self.download_btn = discord.ui.Button(label="📥", style=discord.ButtonStyle.link, url=file_url, row=0)
                self.add_item(self.download_btn)

            embed = discord.Embed(title=f"🎬 Vidéo #{post_id}", url=post_url, color=0x9B59B6)
            embed.add_field(name="👁️ Vues", value=str(view_count), inline=True)
            embed.add_field(name="💰 Waifame", value=f"+{earned} ({total_waifame} total)", inline=True)
            embed.add_field(name="⏳", value="Chargement...", inline=True)
            embed.set_footer(text=f"Tags: {post.get('tag_string', '')[:50]}...")
            
            await interaction.message.edit(embed=embed, view=self)
            
            # Delete old video message
            if self.video_message:
                try:
                    await self.video_message.delete()
                except:
                    pass
            
            # Download and upload new video as attachment
            try:
                resp = requests.get(file_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200 and len(resp.content) < 8_000_000:  # 8MB limit
                    video_file = discord.File(io.BytesIO(resp.content), filename=f"video_{post_id}.{file_ext}")
                    self.video_message = await interaction.channel.send(file=video_file)
                else:
                    self.video_message = await interaction.channel.send(f"📹 Vidéo trop volumineuse: {file_url}")
            except Exception as e:
                print(f"Video download error: {e}")
                self.video_message = await interaction.channel.send(f"📹 {file_url}")
            
            # Update embed to remove loading
            embed.remove_field(2)  # Remove the loading field
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.followup.send("Erreur: Aucune vidéo trouvée avec ces tags.", ephemeral=True)

    async def rewind_callback(self, interaction: discord.Interaction):
        if not await self.check_user(interaction): return
        if self.guild_id in video_history and len(video_history[self.guild_id]) > 1:
            video_history[self.guild_id].pop()
            prev_post = video_history[self.guild_id][-1]
            self.post = prev_post
            
            file_url = prev_post.get('file_url')
            post_id = prev_post.get('id')
            file_ext = prev_post.get('file_ext', 'mp4')

            embed = discord.Embed(title=f"🎬 Vidéo #{post_id}", url=f"https://danbooru.donmai.us/posts/{post_id}", color=0x9B59B6)
            embed.add_field(name="📼", value="Vidéo précédente", inline=True)
            embed.set_footer(text=f"Tags: {prev_post.get('tag_string', '')[:50]}...")
            
            await interaction.response.edit_message(embed=embed, view=self)
            
            # Delete old video message
            if self.video_message:
                try:
                    await self.video_message.delete()
                except:
                    pass
            
            # Download and upload previous video
            try:
                resp = requests.get(file_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200 and len(resp.content) < 8_000_000:
                    video_file = discord.File(io.BytesIO(resp.content), filename=f"video_{post_id}.{file_ext}")
                    self.video_message = await interaction.channel.send(file=video_file)
                else:
                    self.video_message = await interaction.channel.send(f"📹 {file_url}")
            except:
                self.video_message = await interaction.channel.send(f"📹 {file_url}")
        else:
            await interaction.response.send_message("Rien à revenir.", ephemeral=True)

class FavoritesView(discord.ui.View):
    """View for browsing user's private favorites list"""
    def __init__(self, user_id, index=0):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.index = index

        self.prev_btn = discord.ui.Button(label="◀️ Précédent", style=discord.ButtonStyle.gray)
        self.prev_btn.callback = self.prev_callback
        self.add_item(self.prev_btn)

        self.next_btn = discord.ui.Button(label="Suivant ▶️", style=discord.ButtonStyle.blurple)
        self.next_btn.callback = self.next_callback
        self.add_item(self.next_btn)
        
        self.delete_btn = discord.ui.Button(label="🗑️ Supprimer", style=discord.ButtonStyle.red)
        self.delete_btn.callback = self.delete_callback
        self.add_item(self.delete_btn)

        self.update_view()

    def get_user_favs(self):
        return get_user_data(self.user_id).get("favorites", [])

    def update_view(self):
        user_favs = self.get_user_favs()
        self.prev_btn.disabled = (self.index == 0)
        self.next_btn.disabled = (self.index >= len(user_favs) - 1)

    async def prev_callback(self, interaction: discord.Interaction):
        self.index -= 1
        self.update_view()
        await self.show_favorite(interaction)

    async def next_callback(self, interaction: discord.Interaction):
        self.index += 1
        self.update_view()
        await self.show_favorite(interaction)
    
    async def delete_callback(self, interaction: discord.Interaction):
        user_favs = self.get_user_favs()
        if 0 <= self.index < len(user_favs):
            removed = user_favs.pop(self.index)
            save_user_data()
            
            if len(user_favs) == 0:
                await interaction.response.edit_message(content="Ta liste de favoris est maintenant vide !", embed=None, view=None)
                return
            
            if self.index >= len(user_favs):
                self.index = len(user_favs) - 1
            
            self.update_view()
            await self.show_favorite(interaction, f"🗑️ Image #{removed.get('id')} supprimée")

    async def show_favorite(self, interaction: discord.Interaction, extra_msg=None):
        user_favs = self.get_user_favs()
            
        if 0 <= self.index < len(user_favs):
            post = user_favs[self.index]
            
            file_url = post.get('file_url')
            post_id = post.get('id')
            
            embed = discord.Embed(title=f"❤️ Favori #{post_id}", url=f"https://danbooru.donmai.us/posts/{post_id}", color=0xFF0055)
            embed.set_image(url=file_url)
            footer = f"{self.index + 1}/{len(user_favs)} | Visible uniquement par toi"
            if extra_msg:
                footer = f"{extra_msg} | {footer}"
            embed.set_footer(text=footer)
            
            await interaction.response.edit_message(embed=embed, view=self)

if __name__ == "__main__":
    # Initialize database and load data
    init_db()
    load_user_data()
    print("Starting bot...")
    bot.run(TOKEN)