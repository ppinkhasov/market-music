#!/usr/bin/env python3

#local
from spotify import getSpotify
from market_conditions import marketChanges

## this will later push music/market info 
## to an LCD screen attached to the raspberrypi
#import LCD1602

#pd,np,ml
import pandas as pd
import numpy as np
import keras
from keras.models import Sequential
from keras.layers import Conv2D, MaxPooling2D, AveragePooling2D 
from keras.layers import Dense, Activation, Dropout, Flatten
from keras.preprocessing import image
from keras.preprocessing.image import ImageDataGenerator
import tensorflow as tf



sp = getSpotify()
currentsong = sp.current_playback()
objects = ('angry', 'disgust', 'fear', 'happy', 'sad', 'surprise', 'neutral')

playlist_limit = 5 
song_limit_per_playlist = 20
def songs_by_emotion(emotion):
    results = sp.search(q=emotion,type='playlist', limit=playlist_limit)
    return results


song_name = currentsong["item"]["name"]
song_artist = currentsong["item"]["artists"][0]["name"]
print("Now playing {} by {}".format(song_name, song_artist))

current_change, df = marketChanges("SPY")


def destroy():
    pass
if __name__ == "__main__":
    print("done")
