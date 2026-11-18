# 美股訊號燈 build script.  usage: python3 -I build.py <market-data/data dir> <site dir with tpl.html>
import pandas as pd, numpy as np, json, sys, os
D=sys.argv[1]; SITE=sys.argv[2]
SIM_START='2026-11-11'      # forward paper account start
COST=0.001

# ---------- loading ----------
_c={}
def raw(t):
    if t not in _c: _c[t]=pd.read_csv(f'{D}/{t}.csv').dropna(subset=['close']).reset_index(drop=True)
    return _c[t]
CAL=list(raw('SPY').date)
def ser(t,col):
    d=raw(t).set_index('date')
    if col=='tr': s=d.adj_close.fillna(d.close)
    elif col=='op': s=d.open*(d.adj_close.fillna(d.close)/d.close)
    else: s=d[col]
    s=s.reindex(CAL)
    return s if col=='op' else s.ffill()
def ema(x,n): return x.ewm(span=n,adjust=False).mean()
def rsi(c,n=14):
    x=c.diff(); u=x.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); dn=(-x.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/dn)
def basket(ts):
    """equal-weight index from daily returns of whichever members exist on each day"""
    r=pd.DataFrame({t:ser(t,'tr').pct_change() for t in ts})
    m=r.mean(axis=1,skipna=True); first=m.first_valid_index()
    idx=(1+m.loc[first:].fillna(0)).cumprod()*100
    return pd.DataFrame({'date':idx.index,'open':idx.values,'high':idx.values,'low':idx.values,'close':idx.values,'adj_close':idx.values})

# ---------- fast lights ----------
def wedge_pop(h,l,c,e10,e20,ext=1.5,n=10):
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/n,adjust=False).mean()
    extDn=(h<=e10-atr*ext).values; cv,a,b=c.values,e10.values,e20.values
    wp=np.zeros(len(cv),bool); f=False
    for i in range(len(cv)):
        f=f or bool(extDn[i]); w=f and cv[i]>a[i] and cv[i]>b[i]; wp[i]=w
        if w: f=False
    return wp
def analyze(d):
    c=d.close.reset_index(drop=True); e={n:ema(c,n) for n in (10,20,21,50,200)}
    hh=d.high.reset_index(drop=True).fillna(c); ll=d.low.reset_index(drop=True).fillna(c)
    wp=wedge_pop(hh,ll,c,e[10],e[20]); wpi=np.where(wp)[0]; wpago=int(len(c)-1-wpi[-1]) if len(wpi) else None
    a21=c>e[21]; a50=c>e[50]; up=a21&a50
    run=0; i=len(c)-1
    while i>=0 and up.iloc[i]: run+=1; i-=1
    r=rsi(c); rmin=r.iloc[-10:].min()
    if up.iloc[-1]: st='buy' if run<=5 else 'hold'
    elif a21.iloc[-1] or a50.iloc[-1]: st='watch'
    else: st='dip' if (rmin<32 and c.iloc[-1]>e[10].iloc[-1]) else 'sell'
    ret=lambda n:float((c.iloc[-1]/c.iloc[-1-n]-1)*100) if len(c)>n else 0.0
    ma200=c.rolling(200).mean().iloc[-1]
    return dict(st=st,run=run,close=round(float(c.iloc[-1]),2),chg=round(ret(1),2),m1=round(ret(21),1),m3=round(ret(63),1),
      e10=round(float(e[10].iloc[-1]),2),e20=round(float(e[20].iloc[-1]),2),wpago=wpago,e21=round(float(e[21].iloc[-1]),2),e50=round(float(e[50].iloc[-1]),2),
      a21=bool(a21.iloc[-1]),a50=bool(a50.iloc[-1]),a200=bool(c.iloc[-1]>e[200].iloc[-1]),
      long=None if np.isnan(ma200) else bool(c.iloc[-1]>ma200),
      rsi=round(float(r.iloc[-1])),oversold=bool(rmin<32),date=d.date.iloc[-1],
      sp=[round(float(x),2) for x in c.iloc[-60:]],sp21=[round(float(x),2) for x in e[21].iloc[-60:]])

def prev_st(d):
    d=d.iloc[:-1]
    c=d.close.reset_index(drop=True)
    if len(c)<60: return None
    e10,e21,e50=ema(c,10),ema(c,21),ema(c,50); a21=c.iloc[-1]>e21.iloc[-1]; a50=c.iloc[-1]>e50.iloc[-1]
    if a21 and a50:
        run=0;i=len(c)-1
        while i>=0 and c.iloc[i]>e21.iloc[i] and c.iloc[i]>e50.iloc[i]: run+=1;i-=1
        return 'buy' if run<=5 else 'hold'
    if a21 or a50: return 'watch'
    return 'dip' if (rsi(c).iloc[-10:].min()<32 and c.iloc[-1]>e10.iloc[-1]) else 'sell'

BASKETS={'MEM':['MU','SNDK','WDC','STX'],'OPT':['COHR','LITE','CIEN','GLW','AAOI','FN'],'PWR*':['BE','VST','CEG','GEV','ETN','VRT','PWR','NRG'],
 'SEQ':['AMAT','LRCX','KLAC','ASML','TER','AEHR','FORM','ONTO','ACMR','CAMT'],'NEO':['CRWV','NBIS','IREN','APLD','CIFR','WULF','CORZ']}
THEMES=[('SMH','半導體'),('SEQ','半導體設備與測試'),('MEM','記憶體'),('OPT','光通訊'),('NEO','Neocloud'),('PWR*','電力'),('IGV','軟體'),('AIQ','AI'),('BOTZ','機器人'),('QTUM','量子'),('CIBR','資安'),
 ('GDX','金礦'),('SIL','銀礦'),('SLX','鋼鐵'),('URA','鈾與核能'),('JETS','航空'),('IYT','運輸'),('ITA','航太國防'),('TAN','太陽能'),('OIH','油服'),('IHI','醫療器材'),('XBI','生技'),
 ('KBE','銀行'),('KRE','區域銀行'),('XRT','零售'),('ITB','建商'),('KWEB','中概網路'),('SOCL','社群'),('IYZ','電信'),('BTC-USD','比特幣'),('WGMI','比特幣礦工'),('IWM','小型股')]
SECTORS=[('XLK','科技'),('XLV','醫療'),('XLF','金融'),('XLY','非必需消費'),('XLP','必需消費'),('XLE','能源'),('XLI','工業'),('XLB','原物料'),('XLU','公用事業'),('XLRE','房地產'),('XLC','通訊')]
lastUS=CAL[-1]
T=[]
for grp,lst in (('theme',THEMES),('sector',SECTORS)):
    for t,n in lst:
        d=basket(BASKETS[t]) if t in BASKETS else raw(t)
        d=d[d.date<=lastUS].reset_index(drop=True)
        if t=='BTC-USD': d=d[d.date.isin(set(CAL))].reset_index(drop=True)
        a=analyze(d); a.update(t=t,n=n,g=grp,members=BASKETS.get(t),prev=prev_st(d)); T.append(a)
O={'buy':0,'hold':1,'watch':2,'dip':3,'sell':4}; T.sort(key=lambda t:(O[t['st']],-t['m1']))
idx={t:analyze(raw(t)) for t in ('SPY','QQQ')}

ASIA=[('2330.TW','台積電','台灣'),('2454.TW','聯發科','台灣'),('2303.TW','聯電','台灣'),('2408.TW','南亞科','台灣'),('3711.TW','日月光','台灣'),
 ('005930.KS','三星電子','韓國'),('000660.KS','SK 海力士','韓國'),('8035.T','東京威力科創','日本'),('6857.T','愛德萬測試','日本'),('6146.T','Disco','日本'),('6920.T','Lasertec','日本'),('285A.T','鎧俠','日本')]
A=[]
for t,n,c in ASIA:
    a=analyze(raw(t)); A.append(dict(t=t,n=n,c=c,st=a['st'],chg=a['chg'],a21=a['a21'],date=a['date']))

cons=pd.read_csv(f'{D}/_constituents.csv')
secmap={'Information Technology':'科技','Technology':'科技','Health Care':'醫療','Financials':'金融','Consumer Discretionary':'非必需消費','Consumer Staples':'必需消費','Energy':'能源','Industrials':'工業','Materials':'原物料','Utilities':'公用事業','Real Estate':'房地產','Communication Services':'通訊','Telecommunications':'通訊','Finance':'金融','Basic Materials':'原物料','Miscellaneous':'其他'}
S=[];n50=0;nsp=0;seen=set()
extra=[(m,m,'主題成分') for ms in BASKETS.values() for m in ms]
UL=[]
if os.path.exists(f'{D}/_universe.csv'):
    import re as _re
    _uu=pd.read_csv(f'{D}/_universe.csv')
    for _s,_n,_sc in zip(_uu.symbol.astype(str),_uu.name.astype(str),_uu.sector):
        _n=_re.sub(r'\s+(Class [A-Z]\b.*|Common Stock.*|Common Shares.*|Ordinary Shares.*|American Depositary.*|Depositary Shares.*|Voting.*|Subordinate Voting.*)$','',_n).strip()
        UL.append((_s.replace('.','-'),_n,_sc if isinstance(_sc,str) else '其他','u'))
for sym,name,sec,ix in list(zip(cons.symbol,cons.name,cons.sector,cons['index']))+UL+[(m,n,s,'x') for m,n,s in extra]:
    if sym in seen or not os.path.exists(f'{D}/{sym}.csv'): continue
    d=raw(sym)
    if len(d)<220: continue
    seen.add(sym); a=analyze(d)
    if ix in('SP500','both'): nsp+=1; n50+=a['a50']
    S.append(dict(t=sym,n=name,s=secmap.get(sec,sec),st=a['st'],run=a['run'],c=a['close'],chg=a['chg'],e21=a['e21'],e10=a['e10'],e20=a['e20'],wpago=a['wpago'],m3=a['m3'],a200=a['a200'],long=a['long'],date=a['date'],core=ix not in('x','u'),u=ix=='u',prev=prev_st(d) if a['date']==lastUS else None))
breadth=round(n50/nsp*100)
buyAll=sorted([s for s in S if s['st']=='buy' and s['a200'] and s['date']==lastUS],key=lambda s:(not s['core'],-s['m3']))
dipAll=sorted([s for s in S if s['st']=='dip' and s['date']==lastUS],key=lambda s:(not s['core'],-s['m3']))
buy=[x for x in buyAll if x['core']][:6]
dip=[x for x in dipAll if x['core']][:6]
spy=idx['SPY']
mk='red' if (not spy['a200'] or (spy['st']=='sell' and breadth<40)) else ('green' if spy['st'] in('buy','hold') and breadth>=50 else 'yellow')

# ---------- slow defence lights: 200-day line + MCDX ----------
def slow_states(t):
    c=ser(t,'close'); h=ser(t,'high'); l=ser(t,'low')
    ma=c.rolling(200).mean(); rawup=c>ma
    st=pd.Series(np.nan,index=c.index); st[rawup]=1; st[((~rawup)&ma.notna()).rolling(2).sum()==2]=0
    sma_up=st.ffill().fillna(0).astype(bool)
    lo=l.rolling(100).min(); hi=h.rolling(100).max(); step=(hi-lo).replace(0,np.nan)/100
    pc=((c*0.96-lo)/step).clip(0,100); lc=100-((c*1.04-lo)/step).clip(0,100)
    spc=pc.rolling(10).mean(); slc=lc.rolling(10).mean()
    return c,ma,rawup,sma_up,(spc>slc),spc,slc
def equity(t,pos,start,end=None):
    tr=ser(t,'tr'); op=ser(t,'op').fillna(tr)
    hh=pos.shift(1,fill_value=False).astype(float); hp=hh.shift(1,fill_value=0.0)
    r=(1+hp*(op/tr.shift(1)-1).fillna(0))*(1+hh*(tr/op-1).fillna(0))-1-COST*(hh-hp).abs()
    r=r.loc[start:end] if end else r.loc[start:]
    if len(r)==0: return pd.Series([1000.0]),0
    return (1+r).cumprod()*1000,int((hh-hp).abs().loc[start:].sum())
def summ(e,n=None):
    out=dict(v=round(float(e.iloc[-1])),mdd=round(float((e/e.cummax()-1).min()*100),1))
    if n is not None: out['tr']=n
    return out
WIN=[('y10','2016-11-10'),('y5','2021-11-10'),('y2','2024-11-11'),('y1','2025-11-10')]
md=lambda d:f'{int(d[5:7])}/{int(d[8:])}'
DEF=[]
POS={}
for t,n in [('SPY','標普 500'),('QQQ','那斯達克 100'),('SMH','半導體'),('SOXX','半導體'),('IGV','軟體'),('BTC-USD','比特幣')]:
    c,ma,rawup,sma_up,mc_up,spc,slc=slow_states(t)
    both=sma_up|mc_up            # in the market unless BOTH lights are red
    POS[t]=dict(sma=sma_up,mcdx=mc_up,both=both)
    i=len(c)-1
    while i>0 and both.iloc[i]==both.iloc[i-1]: i-=1
    since=c.index[i]; days=len(c)-i
    s_ok,m_ok=bool(sma_up.iloc[-1]),bool(mc_up.iloc[-1])
    if not both.iloc[-1]: lvl='out'
    elif days<=5 and i>0: lvl='buy'
    elif s_ok and m_ok: lvl='safe'
    else: lvl='warn'
    bt={}
    for w,start in WIN:
        hold=ser(t,'tr').loc[start:]
        row=dict(hold=summ(hold/hold.iloc[0]*1000))
        for k in ('sma','mcdx','both'):
            e,ntr=equity(t,POS[t][k],start); row[k]=summ(e,ntr)
        bt[w]=row
    DEF.append(dict(t=t,n=n,lvl=lvl,close=round(float(c.iloc[-1]),2),ma=round(float(ma.iloc[-1]),2),dist=round(float((c.iloc[-1]/ma.iloc[-1]-1)*100),1),
        sma=s_ok,mcdx=m_ok,pc=round(float(spc.iloc[-1])),lc=round(float(slc.iloc[-1])),since=since,days=days,bt=bt,
        sp=[round(float(x),2) for x in c.iloc[-250::5]],spm=[round(float(x),2) for x in ma.iloc[-250::5]]))

def realopen(t,d):
    r=raw(t); x=r[r.date==d]
    return round(float(x.open.iloc[0]),2) if len(x) and not np.isnan(x.open.iloc[0]) else None
# ---------- fast-light theme rotation engine ----------
ROT=['SMH','IGV','AIQ','BOTZ','QTUM','CIBR','GDX','SIL','SLX','URA','JETS','IYT','ITA','TAN','OIH','IHI','XBI','KBE','KRE','XRT','ITB','KWEB','SOCL','IYZ','IBIT','WGMI','IWM']+[t for t,_ in SECTORS]
Cc=pd.DataFrame({t:ser(t,'close') for t in ROT}); TRr=pd.DataFrame({t:ser(t,'tr') for t in ROT}); OPr=pd.DataFrame({t:ser(t,'op') for t in ROT})
have=pd.DataFrame({t:raw(t).set_index('date').close.reindex(CAL).notna() for t in ROT})
UPr=(Cc>ema(Cc,21))&(Cc>ema(Cc,50)); MOM=TRr/TRr.shift(63)-1
def rotation(start,N=5,log=False):
    idxs=[i for i,d in enumerate(CAL) if d>=start]
    if not idxs: return (pd.Series([1000.0]),[],{}) if log else pd.Series([1000.0])
    i0=idxs[0]; cash=1000.0; pos={}; eq=[]; trades=[]
    upv,Mv,trv,opv,hv=UPr.values,MOM.values,TRr.values,OPr.values,have.values; col={t:j for j,t in enumerate(ROT)}
    for i in range(i0,len(CAL)):
        if i>0:
            p=i-1
            for t in list(pos):
                j=col[t]
                if not upv[p,j]:
                    px=opv[i,j] if not np.isnan(opv[i,j]) else trv[i,j]
                    cash+=pos[t]*px*(1-COST); trades.append((CAL[i],'賣出',t)); del pos[t]
            if len(pos)<N:
                cs=sorted([(Mv[p,j],t) for t,j in col.items() if upv[p,j] and hv[p,j] and not np.isnan(Mv[p,j]) and t not in pos],reverse=True)
                equity_=cash+sum(pos[t]*trv[p,col[t]] for t in pos)
                for m,t in cs[:N-len(pos)]:
                    j=col[t]; px=opv[i,j] if not np.isnan(opv[i,j]) else trv[i,j]; amt=min(cash,equity_/N)
                    if amt<1: break
                    pos[t]=amt*(1-COST)/px; cash-=amt; trades.append((CAL[i],'買進',t))
        eq.append(cash+sum(pos[t]*trv[i,col[t]] for t in pos))
    e=pd.Series(eq,index=CAL[i0:])
    return (e,trades,pos) if log else e
def rotation_pending(pos,N=5):
    col={t:j for j,t in enumerate(ROT)}; p=len(CAL)-1
    sells=[t for t in pos if not UPr.values[p,col[t]]]
    cs=sorted([(MOM.values[p,j],t) for t,j in col.items() if UPr.values[p,j] and have.values[p,j] and not np.isnan(MOM.values[p,j]) and t not in pos],reverse=True)
    return dict(sell=sorted(sells),buy=[t for m,t in cs[:N-(len(pos)-len(sells))]])
Hr=pd.DataFrame({t:ser(t,'high') for t in ROT}); Lr=pd.DataFrame({t:ser(t,'low') for t in ROT})
_dip=(Cc<ema(Cc,21))&(Cc<ema(Cc,50))&(Cc.apply(rsi).rolling(10).min()<32)&(Cc>ema(Cc,10))
WPr=_dip&~_dip.shift(1,fill_value=False)&have
def wp_account(start,hold=40,N=5):
    idxs=[i for i,d in enumerate(CAL) if d>=start]
    def pending(pos,i_last):
        sells=[t for t in pos if i_last+1-pos[t]['i']>=hold]
        free=N-(len(pos)-len(sells)); bl=[t for t in ROT if WPr[t].iloc[-1] and t not in pos]
        return dict(sell=sorted(sells),buy=bl[:max(0,free)])
    if not idxs: return pd.Series([1000.0]),{},[],pending({},len(CAL)-1)
    i0=idxs[0]; cash=1000.0; pos={}; eq=[]; log=[]; trv,opv,wv=TRr.values,OPr.values,WPr.values; col={t:j for j,t in enumerate(ROT)}
    for i in range(i0,len(CAL)):
        if i>0:
            p=i-1
            for t in list(pos):
                if i-pos[t]['i']>=hold:
                    j=col[t]; px=opv[i,j] if not np.isnan(opv[i,j]) else trv[i,j]
                    cash+=pos[t]['sh']*px*(1-COST); del pos[t]; log.append((CAL[i],'賣出',t))
            if len(pos)<N:
                eqv=cash+sum(pos[t]['sh']*trv[p,col[t]] for t in pos)
                for t in ROT:
                    j=col[t]
                    if not wv[p,j] or t in pos or len(pos)>=N: continue
                    px=opv[i,j] if not np.isnan(opv[i,j]) else trv[i,j]; amt=min(cash,eqv/N)
                    if amt<1: break
                    pos[t]=dict(sh=amt*(1-COST)/px,i=i); cash-=amt; log.append((CAL[i],'買進',t))
        eq.append(cash+sum(pos[t]['sh']*trv[i,col[t]] for t in pos))
    return pd.Series(eq,index=CAL[i0:]),pos,log,pending(pos,len(CAL)-1)
fast_smh=(ser('SMH','close')>ema(ser('SMH','close'),21))&(ser('SMH','close')>ema(ser('SMH','close'),50))

# ---------- report card ----------
BENCH=[('SPY','買 SPY 放著'),('QQQ','買 QQQ 放著'),('SOXX','買 SOXX 放著'),('SMH','買 SMH 放著'),('IGV','買 IGV 放著'),('BTC-USD','買比特幣放著')]
def table(start):
    rows=[]
    e,trd,_=rotation(start,log=True); rows.append(dict(n='快速燈：主題輪動',k='s',**summ(e,len(trd))))
    e,n=equity('SMH',fast_smh,start); rows.append(dict(n='快速燈：只買賣半導體',k='s',**summ(e,n)))
    e,n=equity('SMH',POS['SMH']['both'],start); rows.append(dict(n='防守燈：半導體 SMH',k='d',**summ(e,n)))
    e,n=equity('QQQ',POS['QQQ']['both'],start); rows.append(dict(n='防守燈：QQQ',k='d',**summ(e,n)))
    e,_,lg,_=wp_account(start); rows.append(dict(n='抄底：超賣反彈（ETF）',k='s',**summ(e,len(lg))))
    for t,n in BENCH:
        h=ser(t,'tr').loc[start:]
        rows.append(dict(n=n,k='b',**summ(h/h.iloc[0]*1000)) if len(h) else dict(n=n,k='b',v=1000,mdd=0.0))
    return rows
BT=dict(win={w:dict(start=s,rows=table(s)) for w,s in WIN})
# forward paper account
simdays=len([d for d in CAL if d>=SIM_START])
BT['sim']=dict(start=SIM_START,days=simdays,rows=table(SIM_START) if simdays else [dict(n=r['n'],k=r['k'],v=1000,mdd=0.0) for r in BT['win']['y1']['rows']])
e,trd,pos=rotation(SIM_START,log=True)
BT['sim']['holding']=sorted(pos) if simdays else []
BT['sim']['defpos']={t:bool(POS[t]['both'].iloc[-1]) for t in ('SMH','QQQ')}
# radar focus-stock paper account (forward only; no trustworthy backtest because of survivorship bias)
def radar_account(start,N=5):
    syms=[x['t'] for x in S if x['core']]
    Cs=pd.DataFrame({t:ser(t,'close') for t in syms}); Ts=pd.DataFrame({t:ser(t,'tr') for t in syms}); Os=pd.DataFrame({t:ser(t,'op') for t in syms})
    e21=ema(Cs,21); up=(Cs>e21)&(Cs>ema(Cs,50)); fresh=up&~(up.rolling(6).sum()==6); cand=fresh&(Cs>ema(Cs,200)); mom=Ts/Ts.shift(63)-1; below=Cs<e21
    def pending(pos):
        sells=[t for t in pos if below[t].iloc[-1]]
        r=mom.iloc[-1][cand.iloc[-1]].dropna().sort_values(ascending=False)
        return dict(sell=sorted(sells),buy=[x for x in r.index if x not in pos][:N-(len(pos)-len(sells))])
    idxs=[i for i,d in enumerate(CAL) if d>=start]
    if not idxs: return pd.Series([1000.0]),{},[],pending({})
    i0=idxs[0]; cash=1000.0; pos={}; eq=[]; n=[]
    for i in range(i0,len(CAL)):
        if i>0:
            p=i-1
            for t in list(pos):
                if below[t].iloc[p]:
                    px=Os[t].iloc[i] if not np.isnan(Os[t].iloc[i]) else Ts[t].iloc[i]
                    cash+=pos[t]['sh']*px*(1-COST); del pos[t]; n.append((CAL[i],'賣出',t))
            if len(pos)<N:
                r=mom.iloc[p][cand.iloc[p]].dropna().sort_values(ascending=False)
                equity_=cash+sum(pos[t]['sh']*Ts[t].iloc[p] for t in pos)
                for t in [x for x in r.index if x not in pos][:N-len(pos)]:
                    px=Os[t].iloc[i] if not np.isnan(Os[t].iloc[i]) else Ts[t].iloc[i]; amt=min(cash,equity_/N)
                    if amt<1: break
                    pos[t]=dict(sh=amt*(1-COST)/px,d=CAL[i]); cash-=amt; n.append((CAL[i],'買進',t))
        eq.append(cash+sum(pos[t]['sh']*Ts[t].iloc[i] for t in pos))
    return pd.Series(eq,index=CAL[i0:]),pos,n,pending(pos)
er,rpos,rlog,rpend=radar_account(SIM_START)
BT['sim']['rows'].append(dict(n='雷達焦點股（最多 5 檔）',k='s',**summ(er,len(rlog))) if simdays else dict(n='雷達焦點股（最多 5 檔）',k='s',v=1000,mdd=0.0))
BT['sim']['radar']=[dict(t=t,d=rpos[t]['d']) for t in sorted(rpos)]
# what the radar account will buy at the next open (so the user can see it in advance)
BT['sim']['radarNext']=rpend
BT['sim']['rotNext']=rotation_pending(pos if simdays else {})
ew,wpos,wlog,wpend=wp_account(SIM_START)
BT['sim']['wp']=sorted(wpos) if simdays else []
BT['sim']['wpNext']=wpend
# full operation log of every paper account
LOG=[dict(d=d,a='雷達焦點股',s=sd,t=t,p=realopen(t,d)) for d,sd,t in rlog]
LOG+=[dict(d=d,a='快速燈主題輪動',s=sd,t=t,p=realopen(t,d)) for d,sd,t in (trd if simdays else [])]
LOG+=[dict(d=d,a='抄底超賣反彈',s=sd,t=t,p=realopen(t,d)) for d,sd,t in (wlog if simdays else [])]
defNext=[]
for acct,t,posser in (('快速燈半導體','SMH',fast_smh),('防守燈半導體','SMH',POS['SMH']['both']),('防守燈 QQQ','QQQ',POS['QQQ']['both'])):
    hh=posser.shift(1,fill_value=False).astype(int); hp=hh.shift(1,fill_value=0)
    if simdays:
        seg=(hh-hp).loc[SIM_START:]; first=seg.index[0]
        for d,v in seg.items():
            v=int(hh.loc[d]) if d==first else int(v)      # on day one, an open position counts as a buy
            if v!=0: LOG.append(dict(d=d,a=acct,s='買進' if v>0 else '賣出',t=t,p=realopen(t,d)))
    now,held=bool(posser.iloc[-1]),bool(hh.iloc[-1]) if simdays else False
    if now!=held: defNext.append(dict(a=acct,s='買進' if now else '賣出',t=t))
BT['sim']['defNext']=defNext
BT['sim']['log']=sorted(LOG,key=lambda x:(x['d'],x['a']),reverse=True)[:400]
# Minervini trend-template account: hold up to 20 template stocks, strongest 6-month first; sell when a stock fails the template
def mm_account(start,N=20):
    syms=[x['t'] for x in S if x['core']]
    Cs=pd.DataFrame({t:ser(t,'close') for t in syms}); Hs=pd.DataFrame({t:ser(t,'high') for t in syms}); Ls=pd.DataFrame({t:ser(t,'low') for t in syms})
    Ts=pd.DataFrame({t:ser(t,'tr') for t in syms}); Os=pd.DataFrame({t:ser(t,'op') for t in syms})
    alive=pd.DataFrame({t:raw(t).set_index('date').close.reindex(CAL).notna() for t in syms})
    s50,s150,s200=Cs.rolling(50).mean(),Cs.rolling(150).mean(),Cs.rolling(200).mean()
    ok=(s50>s150)&(s150>s200)&(Cs>s50)&(s200>s200.shift(10))&(s200.shift(10)>s200.shift(20))&(s200.shift(20)>s200.shift(30))&(Cs>=1.3*Ls.rolling(252).min())&(Cs>=0.75*Hs.rolling(252).max())&alive
    mom=Ts/Ts.shift(126)-1
    def pending(pos):
        sells=[t for t in pos if not ok[t].iloc[-1]]
        r=mom.iloc[-1][ok.iloc[-1]].dropna().sort_values(ascending=False)
        return dict(sell=sorted(sells),buy=[x for x in r.index if x not in pos][:N-(len(pos)-len(sells))])
    idxs=[i for i,d in enumerate(CAL) if d>=start]
    if not idxs: return pd.Series([1000.0]),{},[],pending({}),int(ok.iloc[-1].sum())
    i0=idxs[0]; cash=1000.0; pos={}; eq=[]; log=[]
    for i in range(i0,len(CAL)):
        if i>0:
            p=i-1
            for t in list(pos):
                if not ok[t].iloc[p]:
                    px=Os[t].iloc[i] if not np.isnan(Os[t].iloc[i]) else Ts[t].iloc[i]
                    cash+=pos[t]*px*(1-COST); del pos[t]; log.append((CAL[i],'賣出',t))
            if len(pos)<N:
                r=mom.iloc[p][ok.iloc[p]].dropna().sort_values(ascending=False)
                eqv=cash+sum(pos[t]*Ts[t].iloc[p] for t in pos)
                for t in [x for x in r.index if x not in pos][:N-len(pos)]:
                    px=Os[t].iloc[i] if not np.isnan(Os[t].iloc[i]) else Ts[t].iloc[i]; amt=min(cash,eqv/N)
                    if amt<1: break
                    pos[t]=amt*(1-COST)/px; cash-=amt; log.append((CAL[i],'買進',t))
        eq.append(cash+sum(pos[t]*Ts[t].iloc[i] for t in pos))
    return pd.Series(eq,index=CAL[i0:]),pos,log,pending(pos),int(ok.iloc[-1].sum())
em,mpos,mlog,mpend,mcount=mm_account(SIM_START)
BT['sim']['rows'].append(dict(n='趨勢模板最強 20 檔',k='s',**summ(em,len(mlog))) if simdays else dict(n='趨勢模板最強 20 檔',k='s',v=1000,mdd=0.0))
BT['sim']['mm']=sorted(mpos) if simdays else []
BT['sim']['mmNext']=mpend; BT['sim']['mmCount']=mcount
LOG+=[dict(d=d,a='趨勢模板',s=sd,t=t,p=realopen(t,d)) for d,sd,t in mlog]
BT['sim']['log']=sorted(LOG,key=lambda x:(x['d'],x['a']),reverse=True)[:400]
# ---------- point-in-time fundamentals (SEC filings via Grok) ----------
_FM={}
def fund_mask(syms):
    key=tuple(syms)
    if key in _FM: return _FM[key]
    _FM[key]=_fund_mask(syms); return _FM[key]
def _fund_mask(syms):
    p=f'{D}/_fundamentals.csv'
    if not os.path.exists(p): return pd.DataFrame(False,index=CAL,columns=syms),{}
    F=pd.read_csv(p).dropna(subset=['report_date']); F['qe']=pd.to_datetime(F.fiscal_quarter_end); F['rd']=pd.to_datetime(F.report_date)
    calD=pd.to_datetime(pd.Series(CAL)).values; cols={}; info={}
    for t,g in F[F.symbol.isin(syms)].groupby('symbol'):
        g=g.sort_values('qe').reset_index(drop=True); rows=[]
        for i in range(len(g)):
            d=(g.qe-(g.qe[i]-pd.Timedelta(days=364))).abs(); j=d.idxmin()
            if d[j]>pd.Timedelta(days=25) or j>=i: continue
            rv,rv0,ni,ni0=g.revenue[i],g.revenue[j],g.net_income[i],g.net_income[j]
            rg=rv/rv0-1 if pd.notna(rv) and pd.notna(rv0) and rv0>0 else np.nan
            turn=bool(pd.notna(ni) and pd.notna(ni0) and ni0<=0 and ni>0)
            nig=ni/ni0-1 if pd.notna(ni) and pd.notna(ni0) and ni0>0 else np.nan
            gm=g.gross_profit[i]/rv if pd.notna(rv) and rv else np.nan; gm0=g.gross_profit[j]/rv0 if pd.notna(rv0) and rv0 else np.nan
            gmd=gm-gm0 if pd.notna(gm) and pd.notna(gm0) else np.nan
            ok=bool(pd.notna(rg) and rg>=0.20 and ((pd.notna(nig) and nig>=0.25) or turn) and (pd.isna(gmd) or gmd>0))
            k=np.searchsorted(calD,np.datetime64(g.rd[i]),side='right')     # usable from the next trading day
            if k<len(CAL): rows.append((CAL[k],ok,dict(q=str(g.qe[i].date()),rg=None if pd.isna(rg) else round(rg*100),nig=None if pd.isna(nig) else round(nig*100),turn=turn,gmd=None if pd.isna(gmd) else round(gmd*100,1))))
        if rows:
            x=pd.Series({d:o for d,o,_ in rows}); x=x[~x.index.duplicated(keep='last')]
            cols[t]=x; info[t]=rows[-1][2]
    m=pd.DataFrame(cols).reindex(CAL).ffill(limit=130).reindex(columns=syms)
    return m.fillna(False).astype(bool),info
# Minervini-style money management account (breakout entry, 7.5% stop, let winners run, concentrated, cash when market weak)
def sepa_account(start,stop=0.075,risk=0.0125,maxpos=8,maxw=0.20,syms=None,univ=None):
    if syms is None: syms=[x['t'] for x in S if x['core']]
    Cs=pd.DataFrame({t:ser(t,'close') for t in syms}); Hs=pd.DataFrame({t:ser(t,'high') for t in syms}); Ls=pd.DataFrame({t:ser(t,'low') for t in syms})
    Vs=pd.DataFrame({t:ser(t,'volume') for t in syms}).fillna(0); Ts=pd.DataFrame({t:ser(t,'tr') for t in syms}); Os=pd.DataFrame({t:ser(t,'op') for t in syms})
    alive=pd.DataFrame({t:raw(t).set_index('date').close.reindex(CAL).notna() for t in syms}); adj=Ts/Cs
    s50,s150,s200=Cs.rolling(50).mean(),Cs.rolling(150).mean(),Cs.rolling(200).mean()
    tt=(s50>s150)&(s150>s200)&(Cs>s50)&(s200>s200.shift(10))&(s200.shift(10)>s200.shift(20))&(s200.shift(20)>s200.shift(30))&(Cs>=1.3*Ls.rolling(252).min())&(Cs>=0.75*Hs.rolling(252).max())&alive
    fm,finfo=fund_mask(syms)
    bo=(tt&(Cs>Hs.rolling(50).max().shift(1))&fm)
    if univ is not None: bo=bo&univ.reindex(index=CAL,columns=syms).fillna(False).astype(bool)
    bo=bo.values; alv=alive.values
    rs=(Ts/Ts.shift(126)-1).values; ma=s50.values; cv,tv,ov,av=Cs.values,Ts.values,Os.values,adj.values
    spyc=ser('SPY','close'); mk=(spyc>spyc.rolling(50).mean()).values
    def exits(pos,p):
        out=[]
        for j,P in pos.items():
            g=cv[p,j]/P['px']-1
            if g>=0.20: P['stop']=max(P['stop'],P['px'])
            if g>=0.50: P['stop']=max(P['stop'],P['px']*1.25)
            if (p+1<len(CAL) and not alv[p+1,j]) or cv[p,j]<P['stop'] or (p+1-P['i']>=10 and cv[p,j]<ma[p,j]): out.append(j)
        return out
    def entries(pos,p,n_free):
        if not mk[p] or n_free<=0: return []
        c=sorted([(rs[p,j],j) for j in np.where(bo[p])[0] if j not in pos and not np.isnan(rs[p,j]) and (p+1>=len(CAL) or alv[p+1,j])],reverse=True)
        return [j for _,j in c[:min(3,n_free)]]
    idxs=[i for i,d in enumerate(CAL) if d>=start]
    cash=1000.0; pos={}; eq=[]; log=[]
    for i in idxs:
        p=i-1
        for j in exits(pos,p):
            px=ov[i,j] if alv[i,j] and not np.isnan(ov[i,j]) else tv[p,j]; cash+=pos[j]['sh']*px*(1-COST); del pos[j]; log.append((CAL[i],'賣出',syms[j]))
        eqv=cash+sum(P['sh']*tv[p,j] for j,P in pos.items())
        for j in entries(pos,p,maxpos-len(pos)):
            px=ov[i,j] if np.isfinite(ov[i,j]) else tv[i,j]; amt=min(cash,eqv*min(maxw,risk/stop))
            if amt<5: break
            raw_px=px/av[p,j]; pos[j]=dict(sh=amt*(1-COST)/px,px=raw_px,stop=raw_px*(1-stop),i=i); cash-=amt; log.append((CAL[i],'買進',syms[j]))
        eq.append(cash+sum(P['sh']*tv[i,j] for j,P in pos.items()))
    last=len(CAL)-1; import copy
    pp=copy.deepcopy(pos); sells=exits(pp,last)
    pend=dict(sell=sorted(syms[j] for j in sells),buy=[syms[j] for j in entries({k:v for k,v in pp.items() if k not in sells},last,maxpos-(len(pos)-len(sells)))])
    hold=[dict(t=syms[j],buy=round(P['px'],2),stop=round(P['stop'],2),now=round(float(cv[last,j]),2)) for j,P in sorted(pos.items(),key=lambda x:syms[x[0]])]
    e=pd.Series(eq if eq else [1000.0],index=[CAL[i] for i in idxs] if eq else [CAL[-1]])
    both=(tt&fm).iloc[-1]; h50=Hs.rolling(50).max().iloc[-1]; nm={**UNAME,**{x['t']:x['n'] for x in S}}
    cands=[]
    for t in both[both].index:
        j=syms.index(t); trig=float(h50[t]); c=float(cv[last,j])
        cands.append(dict(t=t,n=nm.get(t,t),c=round(c,2),trig=round(trig,2),gap=round((trig/c-1)*100,1),stop=round(trig*(1-stop),2),bo=bool(bo[last,j]),f=finfo.get(t,{}),rs=None if np.isnan(rs[last,j]) else round(float(rs[last,j])*100)))
    cands.sort(key=lambda x:(not x['bo'],x['gap']))
    return e,hold,log,pend,bool(mk[last]),int(bo[last].sum()),int(both.sum()),finfo,cands
# scan universe (forward) and point-in-time membership (backtest)
def _ok(t): return os.path.exists(f'{D}/{t}.csv') and len(raw(t))>=220
CORE=[x['t'] for x in S if x['core']]
UNAME={}
if os.path.exists(f'{D}/_universe.csv'):
    _u=pd.read_csv(f'{D}/_universe.csv'); _u['symbol']=_u.symbol.astype(str).str.replace('.','-',regex=False)
    import re as _re
    UNAME={s:_re.sub(r'\s+(Class [A-Z]\b.*|Common Stock.*|Common Shares.*|Ordinary Shares.*|American Depositary.*|Depositary Shares.*|Voting.*|Subordinate Voting.*)$','',str(n)).strip() for s,n in zip(_u.symbol,_u.name)}
    USYMS=sorted(set(CORE)|{s for s in _u.symbol if _ok(s)})
else: USYMS=CORE
PIT=None
if os.path.exists(f'{D}/_membership.csv'):
    _m=pd.read_csv(f'{D}/_membership.csv'); _m['f']=_m.symbol.astype(str).str.replace('.','-',regex=False)
    _m=_m[_m.f.apply(_ok)&~(_m.f.isin({'ATI','CNX','PCG','Q','SE','BATRA','BATRK'})&_m.date_removed.notna())]
    PSYMS=sorted(set(_m.f)); _cd=pd.to_datetime(pd.Series(CAL)).values; PIT=pd.DataFrame(False,index=CAL,columns=PSYMS)
    for _,r in _m.iterrows():
        a=np.datetime64(pd.to_datetime(r.date_added)) if pd.notna(r.date_added) else np.datetime64('1900-01-01')
        b=np.datetime64(pd.to_datetime(r.date_removed)) if pd.notna(r.date_removed) else np.datetime64('2100-01-01')
        PIT.loc[(_cd>=a)&(_cd<b),r.f]=True
es,shold,slog,spend,smk,sbo,sboth,FINFO,SCANDS=sepa_account(SIM_START,syms=USYMS)
BT['sim']['sepaN']=len(USYMS)
BT['sim']['rows'].append(dict(n='小賠大賺（基本面＋突破）',k='s',**summ(es,len(slog))) if simdays else dict(n='小賠大賺（基本面＋突破）',k='s',v=1000,mdd=0.0))
BT['sim']['sepa']=shold; BT['sim']['sepaNext']=spend; BT['sim']['sepaMkt']=smk; BT['sim']['sepaBO']=sbo; BT['sim']['sepaBoth']=sboth; BT['sim']['sepaCands']=SCANDS
LOG+=[dict(d=d,a='小賠大賺',s=sd,t=t,p=realopen(t,d)) for d,sd,t in slog]
for w,st in WIN:
    if PIT is not None: r=sepa_account(st,syms=PSYMS,univ=PIT); BT['win'][w]['rows'].insert(4,dict(n='小賠大賺（用當時的成分股）',k='s',**summ(r[0],len(r[2]))))
    else: r=sepa_account(st); BT['win'][w]['rows'].insert(4,dict(n='小賠大賺（基本面＋突破，個股）',k='s',**summ(r[0],len(r[2])),bias=True))
BT['sim']['log']=sorted(LOG,key=lambda x:(x['d'],x['a']),reverse=True)[:400]
# 2026 semiconductor case study (fast light)
c=ser('SMH','close'); tr=ser('SMH','tr'); op=ser('SMH','op'); trs=[]; ent=None
for i in range(1,len(CAL)):
    px=float(op.iloc[i]/tr.iloc[i]*c.iloc[i]) if not np.isnan(op.iloc[i]) else float(c.iloc[i])
    if ent is None and fast_smh.iloc[i-1]: ent=(CAL[i],px)
    elif ent is not None and not fast_smh.iloc[i-1]:
        trs.append(dict(b=ent[0],bp=round(ent[1],1),s=CAL[i],sp=round(px,1),r=round((px/ent[1]-1)*100,1))); ent=None
y=c[c.index>='2026-01-02']; ee,_=equity('SMH',fast_smh,'2026-01-02')
both=POS['SMH']['both'].loc[y.index]
BT['case']=dict(d=list(y.index),c=[round(float(x),1) for x in y],up=[bool(x) for x in fast_smh.loc[y.index]],slow=[bool(x) for x in both],
    trades=[t for t in trs if t['s']>='2026-01-01'],bh=round(float((y.iloc[-1]/y.iloc[0]-1)*100),1),sg=round(float(ee.iloc[-1]/10-100),1))
es,_=equity('SMH',POS['SMH']['both'],'2026-01-02'); BT['case']['sl']=round(float(es.iloc[-1]/10-100),1)
ch=both.astype(int).diff(); BT['case']['slowEv']=[dict(d=d,up=bool(v>0),p=round(float(c[d]),1)) for d,v in ch.items() if v!=0 and not np.isnan(v)]
BT['robust']=dict(n=264,beatQQQ=6)

upd=open(f'{D}/_updated.txt').read().split('\n')[:3]
out=dict(date=lastUS,asiaDate=A[0]['date'],built=pd.Timestamp.now(tz='Asia/Kuala_Lumpur').strftime('%Y-%m-%d %H:%M'),mk=mk,breadth=breadth,idx={k:{x:v[x] for x in('st','chg')} for k,v in idx.items()},
    defence=DEF,themes=T,asia=A,stocks=S,buy=[b['t'] for b in buy],dip=[b['t'] for b in dip],buyMore=[b['t'] for b in buyAll if not b['u'] and b not in buy],dipMore=[b['t'] for b in dipAll if not b['u'] and b not in dip],counts={k:sum(1 for s in S if s['st']==k) for k in O})
js=lambda o:json.dumps(o,ensure_ascii=False,separators=(',',':')).replace('</','<\\/')
html=open(f'{SITE}/tpl.html').read().replace('__DATA__',js(out)).replace('__BT__',js(BT))
open(f'{SITE}/index.html','w').write(html)
json.dump(dict(out=out,bt=BT),open(f'{SITE}/debug.json','w'),ensure_ascii=False)
print('OK',lastUS,'asia',A[0]['date'],mk,breadth,len(S),'stocks',len(html),'bytes')
for x in DEF: print('DEF',x['t'],x['lvl'],'sma',x['sma'],'mcdx',x['mcdx'],x['since'],x['days'],{w:(x['bt'][w]['hold']['v'],x['bt'][w]['both']['v'],x['bt'][w]['both']['mdd'],x['bt'][w]['both']['tr']) for w in x['bt']})
for w in BT['win']: print(w,[(r['n'],r['v'],r['mdd']) for r in BT['win'][w]['rows']])
print('themes',[(t['n'],t['st']) for t in T if t['t'] in BASKETS or t['t']=='SMH'])
print('case',BT['case']['bh'],BT['case']['sg'],BT['case']['sl'],BT['case']['slowEv'][-4:])
