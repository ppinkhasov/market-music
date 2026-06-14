import spotipy
import spotipy.util as util
from spotipy.oauth2 import SpotifyClientCredentials

import os
from dotenv import load_dotenv

load_dotenv()
 
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_USERNAME = os.getenv("SPOTIFY_USERNAME")

def getSpotify():

    # for avaliable scopes see https://developer.spotify.com/web-api/using-scopes/
    scope = "user-library-read playlist-modify-public playlist-read-private app-remote-control streaming user-read-recently-played user-read-playback-position user-read-playback-state user-modify-playback-state user-read-currently-playing"
    redirect_uri = "http://localhost:8888/callback"

    client_credentials_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
    )

    sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)

    token = util.prompt_for_user_token(
        SPOTIFY_USERNAME, scope, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, redirect_uri
    )

    if token:
        sp = spotipy.Spotify(auth=token)
        return sp
    else:
        print("Can't get token for", username)
