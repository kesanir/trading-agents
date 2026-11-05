"""One immutable QQQ snapshot -> explicit gamma/range/scenario report data.

Call-positive/put-negative OI is a positioning assumption, not observed dealer inventory.
No orders, trade intents, signal files, or LLM-generated numerical levels.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import erf, exp, isfinite, log, pi, sqrt
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PT = ZoneInfo('America/Los_Angeles')


def gamma(spot, strike, years, iv):
    if min(spot, strike, years, iv) <= 0:
        return 0.0
    d1 = (log(spot / strike) + .5 * iv * iv * years) / (iv * sqrt(years))
    return exp(-.5 * d1 * d1) / sqrt(2 * pi) / (spot * iv * sqrt(years))


def delta(spot, strike, years, iv, side):
    d1 = (log(spot / strike) + .5 * iv * iv * years) / (iv * sqrt(years))
    value = .5 * (1 + erf(d1 / sqrt(2)))
    return value if side == 'call' else value - 1


def nq_level(qqq_level, qqq_price, nq_price):
    if qqq_level is None:
        return None
    value = Decimal(str(qqq_level)) * Decimal(str(nq_price)) / Decimal(str(qqq_price))
    return float((value / Decimal('.25')).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * Decimal('.25'))


def portfolio_gex(contracts, spot, elapsed_seconds=0):
    """Signed dollar gamma per 1% move, at hypothetical spot/time, fixed OI/IV."""
    return sum(gamma(spot, c['strike'], max(0, c['years']-elapsed_seconds/(365*86400)), c['iv']) *
               c['oi'] * 100 * spot**2 * .01 * (1 if c['side'] == 'call' else -1)
               for c in contracts)


def spot_sweep(contracts, spot):
    """Reprice fixed-IV contracts over +/-15%; interpolate all net-gamma roots."""
    def exposure(price):
        return portfolio_gex(contracts, price)
    prices = [spot * (.85 + .30 * i / 300) for i in range(301)]
    values = [exposure(p) for p in prices]
    roots = []
    for i in range(1, len(prices)):
        a, b = values[i-1], values[i]
        if a * b < 0:
            lo, hi = prices[i-1], prices[i]
            for _ in range(28):
                mid = (lo + hi) / 2
                if exposure(lo) * exposure(mid) <= 0:
                    hi = mid
                else:
                    lo = mid
            roots.append(round((lo + hi) / 2, 4))
        elif a == 0 and b != 0:
            roots.append(prices[i-1])
    return {'prices': prices, 'net_gex': values, 'crossings': roots,
            'nearest': min(roots, key=lambda p: abs(p-spot)) if roots else None,
            'method': 'Fixed-IV Black-Scholes spot sweep; +/-15%; call + / put -; r=q=0'}


def gamma_asymmetry(contracts, spot, seconds_to_cash_close):
    """Scenario sensitivities, never observed or guaranteed hedge orders.

    Finite-move hedge adjustment = -signed change in option delta * OI * 100 *
    destination spot. Later GEX holds spot, OI and IV fixed and rolls time only.
    """
    rows = []
    elapsed = max(0, seconds_to_cash_close - 30*60)
    for change in (-.01, 0, .01):
        hypothetical = spot * (1+change)
        hedge = -sum((1 if c['side']=='call' else -1) * c['oi'] * 100 * hypothetical *
                     (delta(hypothetical,c['strike'],c['years'],c['iv'],c['side']) -
                      delta(spot,c['strike'],c['years'],c['iv'],c['side'])) for c in contracts)
        rows.append({'change':change,'spot':hypothetical,
                     'gex_b':portfolio_gex(contracts,hypothetical)/1e9,
                     'later_gex_b':portfolio_gex(contracts,hypothetical,elapsed)/1e9,
                     'hedge_adjustment_b':hedge/1e9})
    down, current, up = rows
    if max(abs(row['gex_b']) for row in rows) < 1e-9:
        headline = 'Exposure is near zero; no directional gamma conclusion.'
    elif down['gex_b'] < 0 and up['gex_b'] < 0:
        if down['gex_b'] < up['gex_b']:
            headline = 'Both sides amplify; downside is more exposed.'
        elif up['gex_b'] < down['gex_b']:
            headline = 'Both sides amplify; upside is more exposed.'
        else:
            headline = 'Both sides carry similar amplification risk.'
    elif down['gex_b'] < 0 <= up['gex_b']:
        headline = 'Downside amplifies; upside is cushioned in the model.'
    elif up['gex_b'] < 0 <= down['gex_b']:
        headline = 'Upside amplifies; downside is cushioned in the model.'
    else:
        headline = 'Both sides are modeled as absorbing moves.'
    return {'headline':headline,'points':rows,'later_elapsed_seconds':elapsed,
            'method':'Fixed-IV/OI delta-hedge proxy; positive adjustment=buy, negative=sell. '
                     'Not actual dealer inventory or observed flow.'}


def build_playbook(scenarios, levels, downside, spot, nq, whole_net, expiry_net):
    """Reference-inspired futures trigger table and regime-dependent avoid panel."""
    bull, bear = scenarios[1:]
    candidates = [('Confirmed loss',bear['entry'],bear['stop'],bear['targets'],'short')]
    # A second breakdown follows the lower structural pocket when one exists.
    pockets = [v for v in [levels['put_wall'],*downside] if v is not None and v < bear['entry']]
    if pockets:
        floor = levels['put_wall'] if levels['put_wall'] in pockets else max(pockets)
        targets = [v for v in downside if v < floor][:2]
        if targets:
            candidates.append(('Confirmed loss',floor,bear['entry'],targets,'short'))
    candidates.append(('Confirmed reclaim',bull['entry'],bull['stop'],bull['targets'],'long'))
    rows = []
    for label,entry,stop,targets,direction in candidates:
        mapped_entry,mapped_stop = nq_level(entry,spot,nq),nq_level(stop,spot,nq)
        mapped_targets = [nq_level(v,spot,nq) for v in targets]
        risk = abs(mapped_entry-mapped_stop)
        reward = abs(mapped_targets[0]-mapped_entry) if mapped_targets else None
        rr = reward/risk if reward is not None and risk > 0 else None
        rows.append({'label':label,'direction':direction,'qqq_trigger':entry,
                     'nq_trigger':mapped_entry,'qqq_stop':stop,
                     'nq_stop':mapped_stop,'qqq_targets':targets,
                     'nq_targets':mapped_targets,
                     'reference_rr':rr,'status':'WAIT: reference below 1.5R' if rr is None or rr<1.5
                                                      else 'Conditional; verify actual fill / retest'})
    avoid = []
    if whole_net < 0:
        avoid.append('Fading a confirmed break. Negative modeled whole-board gamma can amplify either direction; '
                     'wait for failed acceptance before considering a fade.')
        avoid.append('Assuming premium selling has an edge. Negative gamma is not a pin setup; '
                     'no IV-versus-realized-volatility or spread-cost edge has been established.')
    else:
        avoid.append('Chasing a wall on its first touch. Positive modeled whole-board gamma can dampen moves; '
                     'require acceptance and a successful retest.')
        avoid.append('Treating positive gamma as permission to sell premium. No option premium or '
                     'IV-versus-realized-volatility edge has been established.')
    if expiry_net >= 0 > whole_net:
        avoid.append('Calling a near-expiry reclaim a whole-board flip. Near-expiry gamma is positive while '
                     'the loaded board remains negative.')
    elif whole_net >= 0 > expiry_net:
        avoid.append('Ignoring the nearest expiry. It remains negative even while the loaded board is positive; '
                     'short-lived intraday expansion is still possible.')
    else:
        avoid.append('Confusing the two switches. A nearest-expiry crossing and a whole-board crossing '
                     'describe different option sets and may move independently.')
    avoid.append('Anchoring on a floor, cap or switch as a destination. Walls are concentrations, '
                 'not guaranteed reversals. Skip stale prices and setups below 1.5R at the actual fill.')
    return {'rows':rows,'avoid':avoid,
            'confirmation':'Breaks, not tests: completed 5-minute close, then retest and break of the retest-bar extreme. '
                           'Use the applicable session VWAP for confirmation.',
            'sizing':'Start small. Structural stops below are references, not position sizes. '
                     'Size NQ/MNQ from the actual broker contract, stop distance and your dollar risk limit.'}


def collect_options(spot, now):
    from app.data.alpaca_client import fetch_oi_map, iter_options_chain, OPTION_FEED
    from app.features.option_time import expiry_years
    from app.services.session_calendar import bounds
    previous_day = now.astimezone(ET).date() - timedelta(days=1)
    while bounds(previous_day) is None:
        previous_day -= timedelta(days=1)
    prior_open = bounds(previous_day)[0]
    oi = fetch_oi_map('QQQ', min_dte=0, max_dte=45, strict=True)
    if not oi:
        raise RuntimeError('QQQ open interest unavailable; no gamma map generated')
    contracts, total, covered, quotes = [], 0, 0, []
    for item in iter_options_chain('QQQ', min_dte=0, max_dte=45, oi_map=oi, strict=True):
        det = item['details']
        years = expiry_years(det['expiration_date'], now, symbol='QQQ')
        if years <= 0:
            continue
        total += 1
        interest = item.get('open_interest')
        if interest is not None:
            covered += 1
        iv = item.get('implied_volatility')
        # Do not quietly treat a default volatility table as market-implied data.
        if interest is None or not iv or not isfinite(iv) or not .001 < iv <= 3:
            continue
        stamp = item.get('quote_timestamp')
        try:
            quote_time = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if quote_time.tzinfo is None or not prior_open <= quote_time <= now + timedelta(minutes=1):
                continue
            oi_date = date.fromisoformat(item['oi_date'])
            if not previous_day <= oi_date <= now.astimezone(ET).date():
                continue
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        quotes.append(stamp)
        contracts.append({'symbol': det['ticker'], 'strike': det['strike_price'],
                          'expiry': det['expiration_date'], 'side': det['contract_type'],
                          'oi': interest, 'iv': iv, 'years': years,
                          'iv_source': 'Alpaca snapshot impliedVolatility',
                          'quote_timestamp': stamp, 'oi_date': item.get('oi_date')})
    if total < 20 or covered / total < .8 or len(contracts) / total < .8:
        raise RuntimeError(f'QQQ options coverage inadequate: {len(contracts)}/{total} usable, '
                           f'{covered}/{total} OI available')
    if not any(c['oi'] > 0 for c in contracts):
        raise RuntimeError('QQQ chain has no positive open interest')
    return contracts, {'feed': OPTION_FEED, 'loaded': total, 'usable': len(contracts),
                       'oi_coverage': covered / total, 'quote_timestamps': quotes,
                       'oldest_quote': min(quotes), 'newest_quote': max(quotes),
                       'oldest_oi_date': min(c['oi_date'] for c in contracts),
                       'iv_policy': 'Market-provided IV only; no default IV table'}


def build_report(market, contracts, quality, now=None):
    from app.features.gex_core import GEXEngine, compute_key_levels
    from app.services.session_calendar import bounds
    from app.data.yahoo_futures import validate_reference
    now = now or datetime.now(timezone.utc)
    for ref in (market['qqq'], market['nq']):
        validate_reference(ref, now)
    if abs((datetime.fromisoformat(market['qqq']['timestamp']) -
            datetime.fromisoformat(market['nq']['timestamp'])).total_seconds()) > 300:
        raise ValueError('Unsynchronized QQQ/NQ references')
    spot, nq = market['qqq']['price'], market['nq']['price']
    today = now.astimezone(ET).date()
    close = bounds(today)[1]
    engine = GEXEngine(underlying='QQQ')
    engine.update_spot(spot, source='yahoo')
    call_map, put_map = {}, {}
    for c in contracts:
        g = gamma(spot, c['strike'], c['years'], c['iv'])
        engine.update_contract(c['symbol'], c['strike'], c['side'], g, c['oi'],
                               expiry=c['expiry'], iv=c['iv'])
        dest = call_map if c['side'] == 'call' else put_map
        dest[c['strike']] = dest.get(c['strike'], 0) + g * 100 * c['oi'] * spot**2 * .01
    strike_map = engine.compute_gex_by_strike()
    levels = compute_key_levels(strike_map, spot)
    # Preserve precision until presentation: rounded totals can disagree with
    # independently repriced asymmetry figures after a second rounding step.
    levels['net_gex_b'] = sum(strike_map.values()) / 1e9
    sweep = spot_sweep(contracts, spot)
    today_contracts = [c for c in contracts if c['expiry'] == today.isoformat()]
    nearest_expiry = min(c['expiry'] for c in contracts)
    expiry_contracts = [c for c in contracts if c['expiry'] == nearest_expiry]
    expiry_sweep = spot_sweep(expiry_contracts,spot)
    expiry_net = portfolio_gex(expiry_contracts,spot)/1e9
    asymmetry = gamma_asymmetry(contracts,spot,(close-now).total_seconds())
    near = [c for c in contracts if c['expiry'] == nearest_expiry and
            abs(c['strike'] / spot - 1) <= .02]
    if not near or not any(c['side'] == 'call' for c in near) or not any(c['side'] == 'put' for c in near):
        raise RuntimeError('Nearest-expiry QQQ ATM IV unavailable')
    # Equal-side medians avoid skew from unequal numbers of call/put observations.
    iv = sum(median(c['iv'] for c in near if c['side'] == s) for s in ('call', 'put')) / 2
    years = max(0, (close - now).total_seconds()) / (365 * 86400)
    move = spot * iv * sqrt(years)
    lower, upper = spot - move, spot + move
    delta_refs = []
    for target in (.10, .15, .25):
        row = {'delta': target}
        for side in ('put', 'call'):
            pool = [c for c in contracts if c['expiry'] == nearest_expiry and c['side'] == side
                    and (c['strike'] < spot if side == 'put' else c['strike'] > spot)]
            if pool:
                c = min(pool, key=lambda c: abs(abs(delta(spot, c['strike'], c['years'], c['iv'], side))-target))
                actual = abs(delta(spot, c['strike'], c['years'], c['iv'], side))
                row[side] = c['strike'] if abs(actual-target) <= .03 else None
                row[side + '_actual_delta'] = actual
            else:
                row[side] = None
        delta_refs.append(row)
    # Retain legacy levels under an honest name; expose separate gross-side walls.
    levels['cumulative_strike_crossing'] = levels.pop('zero_gamma', None)
    levels['call_wall'] = max(call_map, key=call_map.get) if call_map and max(call_map.values()) > 0 else None
    levels['put_wall'] = max(put_map, key=put_map.get) if put_map and max(put_map.values()) > 0 else None
    flip = sweep['nearest']
    upside = sorted(set(v for v in [upper, spot+2*move, spot+3*move, levels['call_wall'], flip,
                                    expiry_sweep['nearest']] if v and v > spot))
    downside = sorted(set(v for v in [lower, spot-2*move, spot-3*move, levels['put_wall'], flip,
                                      expiry_sweep['nearest']] if v and v < spot), reverse=True)
    bull_trigger, bear_trigger = upside[0], downside[0]
    pivot = flip if flip and lower < flip < upper else spot
    scenarios = [
        {'name': 'Range / rotation', 'trigger': f'QQQ remains between {bear_trigger:.2f} and {bull_trigger:.2f}.',
         'confirmation': 'Reject an edge and close back inside on a completed 5-minute bar; avoid the middle.',
         'invalidation': '5-minute acceptance outside an edge. Negative gamma increases expansion risk.',
         'entry': None, 'stop': None, 'targets': [spot], 'direction': 'range'},
        {'name': 'Bullish acceptance', 'trigger': f'5-minute close above QQQ {bull_trigger:.2f}, then successful retest.',
         'confirmation': 'Retest low holds the trigger; break retest-bar high. Prefer above the applicable session VWAP.',
         'invalidation': '5-minute close back below trigger; skip if stop/target ordering is invalid.',
         'entry': bull_trigger, 'stop': min(pivot, spot),
         'targets': [v for v in upside if v > bull_trigger][:2], 'direction': 'long'},
        {'name': 'Bearish acceptance', 'trigger': f'5-minute close below QQQ {bear_trigger:.2f}, then failed reclaim.',
         'confirmation': 'Retest high stays below trigger; break retest-bar low. Prefer below the applicable session VWAP.',
         'invalidation': '5-minute close back above trigger; skip if stop/target ordering is invalid.',
         'entry': bear_trigger, 'stop': max(pivot, spot),
         'targets': [v for v in downside if v < bear_trigger][:2], 'direction': 'short'}]
    references = [('Put wall', levels['put_wall']), ('Expected low', lower),
                  ('Whole-board switch (0-45D)', flip), ('QQQ reference', spot),
                  ('Expected high', upper), ('Call wall', levels['call_wall'])]
    for row in delta_refs:
        references.extend([(f"{row['delta']:.0%} delta put", row['put']),
                           (f"{row['delta']:.0%} delta call", row['call'])])
    warnings = ['OI positioning proxy: calls positive / puts negative; dealer inventory is unobserved.',
                'Yahoo prices may be delayed. Continuous futures basis can differ from your broker contract.',
                'Premarket options quotes/IV may be from the prior session; these are model estimates.',
                'No calibrated scenario probabilities. Delta strikes are not outcome probabilities.',
                'RTH VWAP and opening range are unavailable before 09:30 ET. Globex VWAP is labeled separately.',
                'Scheduled economic events are not integrated; check the calendar before any setup.']
    if not today_contracts:
        warnings.append('Strict 0DTE unavailable; nearest expiry shown explicitly, without substitution.')
    if not flip:
        warnings.append('No gamma switch found inside the +/-15% spot sweep.')
    playbook = build_playbook(scenarios,levels,downside,spot,nq,levels['net_gex_b'],expiry_net)
    return {'schema_version': 2, 'date': today.isoformat(), 'as_of': now.isoformat(),
            'market': market, 'quality': quality, 'levels': levels, 'sweep': sweep,
            'expiry_sweep':expiry_sweep,'nearest_expiry':nearest_expiry,
            'expiry_net_gex_b':expiry_net,'asymmetry':asymmetry,'playbook':playbook,
            'gex_by_strike': strike_map, 'gex_by_expiry': engine.compute_gex_by_expiry_strike(),
            'buckets': engine.compute_gex_bucketed(),
            'zero_dte_net_gex_b': sum(gamma(spot,c['strike'],c['years'],c['iv']) * c['oi'] *
                                    100 * spot**2 * .01 * (1 if c['side']=='call' else -1)
                                    for c in today_contracts) / 1e9 if today_contracts else None,
            'expected': {'iv': iv, 'expiry': nearest_expiry, 'anchor': spot, 'move': move,
                         'lower': lower, 'upper': upper, 'horizon_end': close.isoformat(),
                         'one_calendar_day_move': spot * iv / sqrt(365),
                         'method': 'QQQ nearest-expiry ATM IV; S*IV*sqrt(calendar time remaining to cash close)'},
            'delta_references': delta_refs, 'scenarios': scenarios, 'warnings': warnings,
            'references': [{'name': name, 'qqq': value, 'nq_mnq': nq_level(value,spot,nq)}
                           for name, value in references],
            'mapping_ratio': nq / spot, 'contracts': contracts}


def collect_report(now=None):
    from app.data.yahoo_futures import get_gamma_market_snapshot
    now = now or datetime.now(timezone.utc)
    market = get_gamma_market_snapshot(now)
    contracts, quality = collect_options(market['qqq']['price'], now)
    return build_report(market, contracts, quality, now)
