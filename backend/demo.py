import time
from sqlalchemy import select
from .models import Account, Thread, Message
from .auth import fernet

def seed(db):
    if db.scalar(select(Account.id)): return
    account = Account(name='Personal', username='alex@hey.com', password=fernet.encrypt(b'demo').decode(), imap_host='demo', smtp_host='demo', smtp_port=587)
    db.add(account); db.flush()
    items = [
        ('A little coffee & a big idea', 'Jamie Chen', 'jamie@studiofield.co', [
            ('Hey Alex,\n\nI’ve been thinking about that idea we talked about last week — a small creative space where people can slow down and make things together.\n\nWould love to pick your brain over coffee sometime. No agenda, just ideas.\n\nJamie', -3),
            ('Hi Jamie,\n\nI love this idea. A space to make things without the pressure to be productive sounds like exactly what people need. Let’s find a time!\n\nAlex', -2),
            ('Hey Alex,\n\nSo glad this resonated with you! How does next Tuesday at 10am sound? There’s a lovely little place on the corner of Maple & 3rd — good coffee, very good banana bread.\n\nI’ll bring a few sketches of the space. Nothing polished, just something to get us dreaming.\n\nLet me know if that works for you. ☕\n\nJamie\n\n—\nJamie Chen\nDesigner & occasional daydreamer\nstudiofield.co', -1)]),
        ('Your October reading list is here', 'The Marginalian', 'hello@themarginalian.org', [('A small collection of big ideas.\n\nThis month: the art of paying attention, a poem for the changing seasons, and a reminder that rest is part of the work.\n\nHappy reading,\nThe Marginalian', -0.8)]),
        ('A quick check-in on the website', 'Sofia Martinez', 'sofia@northstar.design', [('Hi Alex,\n\nThe homepage designs are ready for your thoughts. Could you share feedback on the layout by Friday? Even a few bullet points would help us move forward.\n\nThanks!\nSofia', -0.7)]),
        ('Saturday farmers market?', 'Oliver Brooks', 'oliver@example.com', [('Hey!\n\nFancy a wander around the farmers market this Saturday morning? I’m planning to get there around 9. No worries if you’re busy.\n\nOllie', -0.6)]),
        ('Your receipt from Paper & Press', 'Paper & Press', 'orders@paperandpress.example', [('Thanks for your order, Alex.\n\n1 × Everyday notebook — £18.00\n1 × Fine point pen — £6.00\nTotal: £24.00\n\nYour order is on its way. No action needed.\n\nPaper & Press', -0.5)]),
        ('Book club: picking our next read', 'Priya Shah', 'priya@example.com', [('Hello readers!\n\nWe’re choosing between Piranesi and Small Things Like These for next month. Reply with your vote whenever you have a moment.\n\nSee you soon,\nPriya', -0.4)]),
        ('A small update from the garden', 'Community Garden', 'grow@example.com', [('Hi neighbours,\n\nThe autumn planting day is happening next Sunday. Bring gloves if you have them; we’ll have spare tools and plenty of tea.\n\nEveryone is welcome.\nThe garden team', -0.3)]),
        ('Sunday dinner at ours', 'Emma Wilson', 'emma@example.com', [('Hi Alex,\n\nWe’re making a big pot of something cosy on Sunday. Would you like to come over at 6? Let me know about any food preferences.\n\nLove,\nEmma', -0.2)])]
    for i, (subject, name, sender, bodies) in enumerate(items):
        thread = Thread(account_id=account.id, subject=subject, oldest=time.time() + bodies[0][1]*86400, status='inbox')
        db.add(thread); db.flush()
        for j, (body, days) in enumerate(bodies):
            own = i == 0 and j == 1
            db.add(Message(thread_id=thread.id, account_id=account.id, uid=f'demo-{i}-{j}', uidvalidity='demo',
                           message_id=f'<demo-{i}-{j}@focusmail>', sender=account.username if own else sender,
                           sender_name='Alex Morgan' if own else name, recipient=sender if own else account.username,
                           date=time.time()+days*86400, body=body))
    db.commit()
