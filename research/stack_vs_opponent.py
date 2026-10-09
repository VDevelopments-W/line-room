import sys; sys.path.insert(0,'/home/claude/nfl-trends')
import numpy as np, pandas as pd
from collections import defaultdict
from props import load_players, STATS, pick, bucket_last
df=load_players(refresh_current=False); df=df[df.season<=2025]
res=defaultdict(lambda:[0,0])
for stat,(lab,ladder,pos) in STATS.items():
    L=np.array(ladder)
    h=df[df.position.isin(pos)&df[stat].notna()]
    for pid,g in h.groupby('player_id',sort=False):
        v=g[stat].to_numpy(float); opp=g.opponent_team.to_numpy()
        run=np.zeros(len(L),int); vs=defaultdict(lambda: np.zeros(len(L),int)); vsn=defaultdict(int)
        for i,x in enumerate(v):
            a=pick(run,ladder,5)
            if a:
                t,_=a; j=ladder.index(t); s=vs[opp[i]][j]; n=vsn[opp[i]]
                if n==0: k='no prior meetings'
                elif s>=3: k='also 3+ straight vs opp'
                elif s==2: k='also 2 straight vs opp'
                elif s==n: k='also hit vs opp (1 game)'
                else: k='MISSED vs opp before'
                for key in (k,'all last-5 picks'):
                    res[(stat,key)][0]+= x>=t; res[(stat,key)][1]+=1
                    res[('ALL',key)][0]+= x>=t; res[('ALL',key)][1]+=1
            hit=x>=L
            vs[opp[i]]=np.where(hit,vs[opp[i]]+1,0); vsn[opp[i]]+=1
            run=np.where(hit,run+1,0)
rows=[(s,k,n,round(h/n*100,1)) for (s,k),(h,n) in res.items()]
out=pd.DataFrame(rows,columns=['stat','case','n','hit%']).sort_values(['stat','case'])
print(out.to_string(index=False))
