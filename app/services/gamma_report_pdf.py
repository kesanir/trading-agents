"""Fixed two-page ReportLab layout, with explicit overflow/page-count validation."""
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


def fmt(value, digits=2):
    return 'N/A' if value is None else f'{value:,.{digits}f}'


def summary(report):
    e, l, m = report['expected'], report['levels'], report['market']
    return (f"QQQ GAMMA MAP | {report['date']} | 06:00 Pacific\n"
            f"QQQ {fmt(m['qqq']['price'])} | Yahoo NQ=F {fmt(m['nq']['price'])}\n"
            f"Gamma: {l['regime']} | Net GEX ${l['net_gex_b']:.3f}B\n"
            f"Put wall {fmt(l['put_wall'])} | Switch {fmt(report['sweep']['nearest'])} | "
            f"Call wall {fmt(l['call_wall'])}\n"
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
        'thead': ParagraphStyle('thead', fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.white)}
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
    story += [table(rows,[228,142,142]), PageBreak(), p('SCENARIOS & FUTURES LEVELS','title'),
              p('Wait for completed 5-minute bars. No scenario percentages are assigned.')]
    from app.services.gamma_report_service import nq_level
    for s in report['scenarios']:
        def pair(value):
            return f"QQQ {fmt(value)} / NQ {fmt(nq_level(value,spot,m['nq']['price']))}"
        targets = '; '.join(pair(v) for v in s['targets']) or 'No next structural target; stand aside.'
        block = [p(s['name'],'heading'),p(s['trigger']),p(s['confirmation']),
                 p(f"Entry reference: {pair(s['entry'])} | Structural stop: {pair(s['stop'])}") if s['entry'] else
                 p('Entry and stop: wait for edge rejection; use the observed rejection-bar extreme.'),
                 p(f'Target references: {targets}'),p(f"Invalidation: {s['invalidation']}",'small')]
        story.append(KeepTogether(block))
    story += [p('Session context & no-trade rules','heading'),
              p(f"NQ overnight high / low: {fmt(m['nq_overnight_high'])} / {fmt(m['nq_overnight_low'])} | "
                f"Globex VWAP (18:00 ET anchor): {fmt(m['nq_globex_vwap'])}."),
              p('Skip stale inputs, failed retests, invalid stop ordering, or targets offering less than 1.5R from the actual entry. '
                'A counter-VWAP rejection has lower confirmation. Recalculate risk from the traded contract and actual fill.'),
              p('Data & methodology','heading'),
              p(f"QQQ quote: {m['qqq']['timestamp']} | NQ quote: {m['nq']['timestamp']}",'small'),
              p(f"Options: Alpaca {report['quality']['feed']} | Usable {report['quality']['usable']}/"
                f"{report['quality']['loaded']} | OI coverage {report['quality']['oi_coverage']:.0%}. "
                'Options timestamps and OI dates are retained in the JSON audit snapshot.','small'),
              p(f"Options quote window: {report['quality'].get('oldest_quote', 'N/A')} to "
                f"{report['quality'].get('newest_quote', 'N/A')} | Oldest OI date: "
                f"{report['quality'].get('oldest_oi_date', 'N/A')}",'small'),
              p(f"Gamma switch: fixed-IV spot sweep +/-15%, r=q=0; {len(report['sweep']['crossings'])} crossing(s). "
                f"Legacy cumulative-strike crossing: {fmt(l['cumulative_strike_crossing'])}. "
                f"Mapping ratio {report['mapping_ratio']:.5f}; NQ/MNQ rounded to 0.25 points.",'small')]
    for warning in report['warnings']:
        story.append(p(warning,'small'))
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
