"""Fixed two-page ReportLab layout, with explicit overflow/page-count validation."""
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


def fmt(value, digits=2):
    return 'N/A' if value is None else f'{value:,.{digits}f}'


def hedge_text(value):
    if abs(value) < .0005:
        return 'Near zero'
    return f"{'BUY' if value > 0 else 'SELL'} ${abs(value):.3f}B"


def strike_text(value):
    return f'{value:,.3f}'.rstrip('0').rstrip('.')


def summary(report):
    e, l, m = report['expected'], report['levels'], report['market']
    return (f"QQQ GAMMA MAP | {report['date']} | 06:00 Pacific\n"
            f"QQQ {fmt(m['qqq']['price'])} | Yahoo NQ=F {fmt(m['nq']['price'])}\n"
            f"Gamma: {l['regime']} | Net GEX ${l['net_gex_b']:.3f}B\n"
            f"Put wall {fmt(l['put_wall'])} | Whole-board switch {fmt(report['sweep']['nearest'])} | "
            f"Call wall {fmt(l['call_wall'])}\n"
            f"Nearest expiry {report['nearest_expiry']}: switch {fmt(report['expiry_sweep']['nearest'])}\n"
            f"{report['asymmetry']['headline']}\n"
            f"Remaining-session implied range: {fmt(e['lower'])} - {fmt(e['upper'])}\n"
            f"Bull: {report['scenarios'][1]['trigger']}\nBear: {report['scenarios'][2]['trigger']}\n"
            f"QQQ quote {m['qqq']['timestamp']}\nNQ quote {m['nq']['timestamp']}\n"
            'Yahoo may be delayed; mapped futures references are approximate. '
            'Conditional scenarios, no calibrated probabilities. See attached two-page report.')


def render_report(report, output):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from pypdf import PdfReader
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    navy, teal, red = colors.HexColor('#153047'), colors.HexColor('#007E87'), colors.HexColor('#B64B48')
    styles = {
        'title': ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=22, leading=25, textColor=navy, spaceAfter=8),
        'heading': ParagraphStyle('heading', fontName='Helvetica-Bold', fontSize=11, leading=14, textColor=navy, spaceBefore=9, spaceAfter=5),
        'body': ParagraphStyle('body', fontName='Helvetica', fontSize=8.5, leading=11, textColor=navy, spaceAfter=4),
        'small': ParagraphStyle('small', fontName='Helvetica', fontSize=7, leading=9, textColor=navy, spaceAfter=3),
        'thead': ParagraphStyle('thead', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.white),
        'banner': ParagraphStyle('banner', fontName='Helvetica', fontSize=9, leading=12, textColor=colors.white),
        'banner_label': ParagraphStyle('banner_label', fontName='Helvetica-Bold', fontSize=8, leading=12,
                                      textColor=colors.HexColor('#A3D4CB'))}
    def p(text, style='body'):
        return Paragraph(escape(str(text)), styles[style])
    def table(rows, widths):
        t = Table([[p(cell, 'thead' if index == 0 else 'small') for cell in row]
                   for index,row in enumerate(rows)], colWidths=widths)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),
                              ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#F0F5F7'),colors.white]),
                              ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),
                              ('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),4),
                              ('BOTTOMPADDING',(0,0),(-1,-1),4)]))
        return t
    m, l, e = report['market'], report['levels'], report['expected']
    stamp = datetime.fromisoformat(report['as_of']).astimezone(ZoneInfo('America/Los_Angeles'))
    story = [p('QQQ / NASDAQ GAMMA MAP', 'title'),
             p('SYNTHETIC EXAMPLE - NOT CURRENT MARKET DATA' if report.get('demo') else
               'Yahoo references may be delayed. Model estimates; verify broker contract.', 'small'),
             p(f"{report['date']} | Prepared {stamp:%H:%M %Z} | Conditional morning plan"),
             table([['QQQ / Yahoo', 'NQ=F / Yahoo', 'Gamma regime / net GEX', 'Strict 0DTE net GEX'],
                    [fmt(m['qqq']['price']), fmt(m['nq']['price']),
                     f"{l['regime'].upper()} / ${l['net_gex_b']:.3f}B",
                     'Unavailable' if report['zero_dte_net_gex_b'] is None else f"${report['zero_dte_net_gex_b']:.3f}B"]], [128]*4),
             p('Gamma exposure by strike', 'heading')]
    # Vector chart: closest 45 strikes around spot, common scale for signed GEX.
    spot = m['qqq']['price']
    items = sorted(((float(k),v) for k,v in report['gex_by_strike'].items()), key=lambda kv: abs(kv[0]-spot))[:45]
    items.sort()
    drawing = Drawing(512, 150)
    baseline = 72
    drawing.add(Line(30, baseline, 502, baseline, strokeColor=colors.HexColor('#C5D0D8')))
    scale = max([abs(v) for _,v in items] or [1]) or 1
    width = 464 / max(len(items),1)
    for i,(strike,value) in enumerate(items):
        height = abs(value) / scale * 55
        x = 34+i*width
        drawing.add(Rect(x,baseline if value>=0 else baseline-height,max(1,width-2),height,
                         fillColor=teal if value>=0 else red,strokeColor=None))
        if i % max(1,len(items)//8) == 0:
            drawing.add(String(x,5,f'{strike:g}',fontSize=6,fillColor=navy))
    drawing.add(String(32,137,f'Peak absolute strike exposure: ${scale/1e6:,.1f}M per 1% move',fontSize=7,fillColor=navy))
    story += [drawing, p('Calls positive / puts negative. Nearest 45 strikes; full chain used for calculations.', 'small'),
              p('Decision levels and approximate futures equivalents', 'heading')]
    rows = [['Reference', 'QQQ', 'NQ / MNQ points']]
    rows += [[r['name'],fmt(r['qqq']),fmt(r['nq_mnq'])] for r in report['references'][:6]]
    from app.services.gamma_report_service import nq_level
    near_switch = report['expiry_sweep']['nearest']
    rows.insert(4,[f"Expiry switch ({report['nearest_expiry']})",fmt(near_switch),
                   fmt(nq_level(near_switch,spot,m['nq']['price']))])
    story += [table(rows,[228,130,154]), p('Expected range and option-delta references', 'heading'),
              p(f"QQQ ATM IV {e['iv']:.2%} | Expiry {e['expiry']} | Remaining to cash close: "
                f"+/-{fmt(e['move'])} ({fmt(e['lower'])} - {fmt(e['upper'])})."),
              p(f"One-calendar-day IV projection: +/-{fmt(e['one_calendar_day_move'])}. "
                'Neither estimate is a guaranteed high/low or a calibrated containment forecast.', 'small')]
    rows = [['Absolute delta / nearest strike', 'Put: QQQ / NQ', 'Call: QQQ / NQ']]
    for idx,d in enumerate(report['delta_references']):
        pr,cr = report['references'][6+idx*2:8+idx*2]
        rows.append([f"{d['delta']:.0%} | expiry {e['expiry']}",
                     f"{fmt(pr['qqq'])} / {fmt(pr['nq_mnq'])}", f"{fmt(cr['qqq'])} / {fmt(cr['nq_mnq'])}"])
    story += [table(rows,[228,142,142]),
              p(f"Nearest-expiry net GEX: ${report['expiry_net_gex_b']:+.3f}B ({report['nearest_expiry']}). "
                'Whole board means the loaded 0-45 DTE set; a switch can be unavailable within the scan.','small'),
              PageBreak(), p('HOW TO PLAY THE STRUCTURE','title')]
    a = report['asymmetry']
    down,current,up = a['points']
    thesis = (f"{a['headline']} Whole-board GEX is ${current['gex_b']:+.3f}B per 1% move. "
              f"A +1% repricing implies {hedge_text(up['hedge_adjustment_b'])}; "
              f"a -1% repricing implies {hedge_text(down['hedge_adjustment_b'])}. "
              f"Nearest-expiry switch: {fmt(near_switch)} ({report['nearest_expiry']}); "
              f"whole-board switch: {fmt(report['sweep']['nearest'])}. "
              'These are fixed-IV/OI inventory proxies, not observed or forced orders.')
    banner = Table([[p('THE ASYMMETRY THAT MATTERS','banner_label'),p(thesis,'banner')]],colWidths=[100,412])
    banner.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),navy),('VALIGN',(0,0),(-1,-1),'TOP'),
                               ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),
                               ('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10)]))
    story += [banner,Spacer(1,6),table([
        ['Whole board / hypothetical spot','QQQ -1%','Reference','QQQ +1%'],
        ['Net GEX now ($B / 1%)',f"{down['gex_b']:+.3f}",f"{current['gex_b']:+.3f}",f"{up['gex_b']:+.3f}"],
        ['30m before cash close ($B / 1%)',f"{down['later_gex_b']:+.3f}",
         f"{current['later_gex_b']:+.3f}",f"{up['later_gex_b']:+.3f}"]],[212,100,100,100]),
        p('Time projection holds OI, IV and each hypothetical spot fixed; it is not a forecast of closing flow.','small')]
    trigger_rows = [['Confirmed trigger / QQQ','NQ / MNQ','Stop / targets (NQ)']]
    for row in report['playbook']['rows']:
        target_text = ' / '.join(fmt(v) for v in row['nq_targets']) or 'N/A'
        rr = 'N/A' if row['reference_rr'] is None else f"{row['reference_rr']:.2f}R"
        trigger_rows.append([f"{row['label']} {fmt(row['qqq_trigger'])}",fmt(row['nq_trigger']),
                             f"Stop {fmt(row['nq_stop'])}; targets {target_text}. {rr}. {row['status']}"])
    left = [p('04  How to play it','heading'),table(trigger_rows,[104,70,146]),
            p(report['playbook']['confirmation'],'small'),p(report['playbook']['sizing'],'small'),
            p('QQQ option suggestions - conditional','heading')]
    option_rows = [['QQQ trigger / expiry','Buy / sell; cap per share','At debit cap: expiry payoff']]
    for idea in report['option_suggestions']['rows']:
        label = f"{'Reclaim' if idea['direction']=='long' else 'Loss'} {fmt(idea['trigger'])}"
        if not idea['available']:
            option_rows.append([label,'No suitable listed spread','N/A - refresh the chain'])
            continue
        side = 'C' if idea['side']=='call' else 'P'
        quote = idea['quote']
        quote_label = 'Quote N/A' if quote['debit'] is None else f"Snap {fmt(quote['debit'])} ({quote['status']})"
        if idea['status'].startswith('WAIT'):
            quote_label += '; WAIT over cap'
        option_rows.append([f"{label}; exp {idea['expiry']}",
                            f"BUY {strike_text(idea['buy_strike'])}{side} / SELL {strike_text(idea['sell_strike'])}{side}. "
                            f"Width {fmt(idea['width'])}; debit cap {fmt(idea['max_debit'])}.",
                            f"Loss ${fmt(idea['max_loss_at_cap'],0)} / gain ${fmt(idea['max_profit_at_cap'],0)}; "
                            f"BE {fmt(idea['breakeven_at_cap'])}; {idea['expiry_rr_at_cap']:.2f}R. {quote_label}."])
    left += [table(option_rows,[80,116,124]),
             p('Nearest listed 1-7 DTE; no 0DTE fallback. Debit cap is not a quote. At-cap payoffs are for one 100-share '
               'intact spread at expiration, before fees; not intraday target returns. Reprice on confirmation; '
               'invalidate on a 5-minute close back across that row\'s trigger. Verify liquidity and close both '
               'legs before expiry. Futures R/R is separate.','small')]
    right = [p('05  What to avoid','heading')]
    for i,avoid in enumerate(report['playbook']['avoid'],1):
        right += [p(f'{i}. {avoid}','small'),Spacer(1,5)]
    right += [table([['CALL WALL','EXPIRY SWITCH','PUT WALL'],
                     [fmt(l['call_wall']),fmt(near_switch),fmt(l['put_wall'])]],[56,56,56])]
    right += [p('Three conditional scenarios','heading'),
              p(f"Range / rotation: {fmt(report['scenarios'][2]['entry'])}-{fmt(report['scenarios'][1]['entry'])}; "
                'invalidate on acceptance outside.','small'),
              p(f"Bullish acceptance: above {fmt(report['scenarios'][1]['entry'])}, retest holds; "
                'invalidate on close back below.','small'),
              p(f"Bearish acceptance: below {fmt(report['scenarios'][2]['entry'])}, failed reclaim; "
                'invalidate on close back above.','small')]
    columns = Table([[left,right]],colWidths=[336,176])
    columns.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(0,0),0),
                                ('RIGHTPADDING',(0,0),(0,0),16),('LEFTPADDING',(1,0),(1,0),4),
                                ('RIGHTPADDING',(1,0),(1,0),4),('BACKGROUND',(1,0),(1,0),colors.HexColor('#F3F1EB'))]))
    story += [columns,p('Session context & data','heading'),
              p(f"NQ overnight high / low: {fmt(m['nq_overnight_high'])} / {fmt(m['nq_overnight_low'])} | "
                f"Globex VWAP (18:00 ET anchor): {fmt(m['nq_globex_vwap'])}. RTH VWAP/range: not yet available.",'small'),
              p(f"QQQ quote: {m['qqq']['timestamp']} | NQ quote: {m['nq']['timestamp']}",'small'),
              p(f"Options: Alpaca {report['quality']['feed']} | Usable {report['quality']['usable']}/"
                f"{report['quality']['loaded']} | OI coverage {report['quality']['oi_coverage']:.0%}. "
                'Options timestamps and OI dates are retained in the JSON audit snapshot.','small'),
              p(f"Options quote window: {report['quality'].get('oldest_quote', 'N/A')} to "
                f"{report['quality'].get('newest_quote', 'N/A')} | Oldest OI date: "
                f"{report['quality'].get('oldest_oi_date', 'N/A')}",'small'),
              p(f"Fixed-IV spot sweeps +/-15%, r=q=0. Board / expiry crossings: "
                f"{len(report['sweep']['crossings'])} / {len(report['expiry_sweep']['crossings'])}. "
                f"Mapping ratio {report['mapping_ratio']:.5f}; ticks 0.25. Calls + / puts - is an inventory assumption.",'small'),
              p('Options may reflect the prior session. Yahoo may be delayed; verify broker basis. '
                'Option debit ceilings are payoff conditions, not price forecasts. '
                'No calibrated scenario probabilities, realized-volatility comparison or max pain. '
                'Economic events are not integrated; check the calendar before trading.','small')]
    def footer(canvas, doc):
        canvas.setStrokeColor(colors.HexColor('#C5D0D8'))
        canvas.line(50,42,562,42)
        canvas.setFont('Helvetica',7)
        canvas.setFillColor(navy)
        canvas.drawString(50,29,'MODEL ESTIMATES | Yahoo may be delayed | Verify broker contract and economic events')
        canvas.drawRightString(562,29,f'{doc.page} / 2')
    doc = SimpleDocTemplate(str(output),pagesize=(612,792),leftMargin=50,rightMargin=50,
                            topMargin=35,bottomMargin=54,title='QQQ Nasdaq Gamma Map',author='Trading Agents')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    if len(PdfReader(output).pages) != 2:
        output.unlink(missing_ok=True)
        raise RuntimeError('Gamma report layout exceeded two pages; publication stopped')
    return output
