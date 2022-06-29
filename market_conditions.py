import pandas as pd
from pandas_datareader import data, wb
from datetime import date

def marketChanges(ticker):
    df = data.get_data_yahoo(ticker)
    df['emotion'] = 6

    #some basic classification to get started
    #(0=Angry, 1=Disgust, 2=Fear, 3=Happy, 4=Sad, 5=Surprise, 6=Neutral).

    #stock market down over a 10 day period and a 2% decline = FEAR (2)
    df['emotion'].loc[(df['Close'].pct_change(periods=10) < 0) & (df['Close'].pct_change() < -0.02)] = 2
    #stock market up over a 10 day period but a 2% decline = DISGUST (1)
    df['emotion'].loc[(df['Close'].pct_change(periods=10) > 0) & (df['Close'].pct_change() < -0.02)] = 1
    #stock market up over a 10 day period and a 2% up move = SURPRISE (5)
    df['emotion'].loc[(df['Close'].pct_change(periods=10) > 0) & (df['Close'].pct_change() > 0.02)] = 5
    #stock market down over a 10 day period and a 1% up move = HAPPY (3)
    df['emotion'].loc[(df['Close'].pct_change(periods=10) < 0) & (df['Close'].pct_change() > 0.01)] = 3
    #stock market down over a 10 day period and a small down move = SAD (4)
    df['emotion'].loc[(df['Close'].pct_change(periods=10) < 0) & (df['Close'].pct_change() < 0.00) & (df['Close'].pct_change() > -0.01)] = 4
    #stock market not with a 2% range on 5 day and intraday period and +/-50bps = NEUTRAL (6)
    df['emotion'].loc[(df['Close'].pct_change(periods=5) < 0.02) & (df['Close'].pct_change(periods=5) < 0.02) & (df['Close'].pct_change() < 0.005) & (df['Close'].pct_change() > -0.005)] = 6
    # cant think of an angry class
    df['emotion'].loc[(df['Close'].pct_change(periods=2) > 0) & (df['Close'].pct_change() < -0.02)] = 0
    

    df["pct_change"] = round(df['Close'].pct_change()*100, 2)
    print(str(df['pct_change'][len(df.index)-1])+"%")

    return df['pct_change'][len(df.index)-1], df

