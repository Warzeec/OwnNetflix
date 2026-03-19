import os
import re
import subprocess
import time

folder = r"C:\Users\openhub\Downloads\OwnNetflix\Abbott.Elementary.S01.COMPLETE.720p.DSNP.WEBRip.x264-GalaxyTV[TGx]"
vlc_path = r"C:\Program Files\VideoLAN\VLC\vlc.exe"

video_ext = (".mp4", ".mkv", ".avi")
progress_file = os.path.join(folder, "progress.txt")


# Extraire numéro épisode
def get_episode_number(filename):
    match = re.search(r"S\d+E(\d+)", filename)
    return int(match.group(1)) if match else -1


# Charger progression
def load_progress():
    try:
        with open(progress_file, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 1


# Sauvegarder progression
def save_progress(ep):
    with open(progress_file, "w") as f:
        f.write(str(ep))


# Trier fichiers
files = [f for f in os.listdir(folder) if f.endswith(video_ext)]
files.sort(key=get_episode_number)

# Charger épisode de départ
saved = load_progress()
choix = input(f"Reprendre à l'épisode {saved} ? (Entrée = oui / sinon tape un numéro) : ").strip()

if choix == "":
    start_episode = saved
else:
    try:
        start_episode = int(choix)
    except ValueError:
        print("Numéro invalide, reprise par défaut.")
        start_episode = saved

nb_episodes = input("Combien d'épisodes ? (Entrée = tous) : ").strip()
if nb_episodes == "":
    max_episodes = len(files)
else:
    try:
        max_episodes = int(nb_episodes)
    except ValueError:
        print("Numéro invalide, on regarde tout.")
        max_episodes = len(files)

shutdown_choice = input("Éteindre le PC après ? (o/N) : ").strip().lower()
shutdown = shutdown_choice == "o"

print(f"▶ Lancement à partir de l'épisode {start_episode} ({max_episodes} épisode(s))")
if shutdown:
    print(f"💤 L'ordinateur s'éteindra après les épisodes")

# Filtrer à partir de là
files = [f for f in files if get_episode_number(f) >= start_episode]
files = files[:max_episodes]

for file in files:
    full_path = os.path.join(folder, file)
    ep_num = get_episode_number(file)

    print(f"\n▶ Lancement : {file}")

    process = subprocess.Popen([
        vlc_path,
        full_path,
        "--fullscreen",
        "--sub-language=fre",
        "--play-and-exit"
    ])

    process.wait()

    print(f"✔ Terminé : {file}")

    # Sauvegarder prochain épisode
    save_progress(ep_num + 1)

    time.sleep(1)

if shutdown:
    print("\n💤 Extinction dans 30 secondes... (annuler : shutdown /a)")
    os.system("shutdown /s /t 30")
else:
    print("\n🎉 Saison terminée !")