M = 'backend/open_webui/socket/main.py'
AUTH = 'backend/open_webui/utils/auth.py'
MFA = 'backend/open_webui/utils/mfa.py'
LAY = 'src/routes/+layout.svelte'
APPLAY = 'src/routes/(app)/+layout.svelte'
SIDE = 'src/lib/components/layout/Sidebar.svelte'
CHAT = 'src/lib/components/chat/Chat.svelte'

SNAPS = [
    dict(
        slug='token-check-hits-db',
        title='כל בדיקת טוקן של סוקט עוברת ב-DB, ותקלה ב-DB נזרקת כשגיאה',
        sub='הבדיקה שכל connect, heartbeat וסריקה תקופתית עוברים דרכה (צילומים 2–4)',
        panels=[
            dict(file=AUTH, ranges=[(576, 586)], marks={579: 1, 582: 2}),
            dict(file=AUTH, ranges=[(272, 281)], marks={279: 3}),
            dict(file=MFA, ranges=[(123, 130)], marks={126: 4}, hot=[127]),
        ],
        notes=[
            (1, 'בדיקת הטוקן קוראת ל-<code>is_valid_token</code>, שמשווה את ה-<code>session_stamp</code> מול ה-DB.'),
            (2, 'ואחר כך עוד שאילתה, <code>Users.get_user_by_id</code>. כלומר לפחות שתי שאילתות DB לכל בדיקה של כל סוקט.'),
            (3, '<code>get_auth</code> שולפת את רשומת ה-auth מה-DB. ה-docstring של הפונקציה אומר במפורש (שורה 275): Database failures fail closed.'),
            (4, 'כשה-pool מלא, ה-timeout של SQLAlchemy נתפס כאן והופך ל-<code>HTTPException(503)</code>. '
                'מכאן והלאה הקוד שקורא לפונקציה <b>לא מבדיל</b> בין "הטוקן לא תקף" לבין "לא הצלחתי לבדוק".'),
        ],
    ),
    dict(
        slug='connect-accepts-invalid-token',
        title='connect: בדיקת DB בכל התחברות, וטוקן פסול מתקבל בשקט כסוקט אנונימי',
        sub='ה-handler שרץ בכל התחברות של סוקט, כולל כל התחברות מחדש',
        panels=[dict(file=M, ranges=[(610, 636)], marks={617: 1, 619: 2})],
        notes=[
            (1, 'כל התחברות מריצה את הבדיקה מצילום 1. כשה-DB תקוע השורה הזו זורקת שגיאה, ובשחזור ראינו שחיבור '
                'ה-WebSocket נקטע והדפדפן מנסה שוב אחרי 1–5 שניות.'),
            (2, 'אין <code>else</code>. טוקן שבוטל (אחרי REVOKE) או טוקן חסר מחזירים <code>None</code>, והחיבור '
                '<b>מתקבל</b> כסוקט בלי משתמש. השרת לא מחזיר <code>connect_error</code>, ולכן הדפדפן לא יודע שהטוקן שלו פסול.'),
        ],
    ),
    dict(
        slug='sweep-and-disconnect-on-any-error',
        title='הסריקה כל 30 שניות, וכל שגיאה בבדיקה מסתיימת בניתוק',
        sub='periodic_socket_authentication ו-get_socket_session_user',
        panels=[dict(file=M, ranges=[(458, 483)], marks={463: 1, 465: 2, 473: 3, 475: 4, 479: 5}, hot=[466, 476])],
        notes=[
            (1, 'כל 30 שניות, באותו קצב בכל 9 הפודים, בלי פיזור (jitter).'),
            (2, 'עוברת ברצף על כל הסוקטים המאומתים של הפוד, וכל אחד מהם נבדק מול ה-DB (צילום 1). עם מאות סוקטים '
                'בפוד זה גל של מאות שאילתות בכל סבב, ובדיוק בזמן שה-pool כבר עמוס.'),
            (3, 'סוקט בלי <code>user</code> (האנונימי מצילום 2) לא עובר את התנאי. הוא לא נכלל בסריקה, אבל '
                'הפונקציה נקראת עבורו גם מ-heartbeat ומ-<code>events:chat</code> (צילום 4), ושם הוא נופל לניתוק.'),
            (4, '<code>except Exception</code> תופס הכול. גם ה-503 של pool מלא נרשם כ"האימות פג", כך שפוד עמוס '
                'מנתק משתמשים שהטוקן שלהם תקין.'),
            (5, 'הניתוק מגיע לדפדפן כ-<code>io server disconnect</code>, ושם הדפדפן מתחבר מחדש מיד (צילום 5).'),
        ],
    ),
    dict(
        slug='heartbeat-and-chat-events',
        title='heartbeat ו-events:chat: כל אירוע מהדפדפן עובר באותה בדיקה',
        sub='גם אירועים רגילים של הלשונית מריצים את get_socket_session_user, כולל הניתוק שבתוכה',
        panels=[
            dict(file=M, ranges=[(723, 732)], marks={725: 1, 732: 2}),
            dict(file=M, ranges=[(866, 870)], marks={868: 3}),
        ],
        notes=[
            (1, 'heartbeat נשלח מכל לשונית כל 30 שניות. כל אחד מהם הוא בדיקת DB (צילום 1), וניתוק אם היא נכשלה.'),
            (2, 'ובנוסף כתיבה ל-DB של <code>last_active_at</code> בכל heartbeat של כל לשונית.'),
            (3, 'גם ה-join שהדפדפן שולח כשצ\'אט פתוח (צילום 7) עובר כאן. לסוקט אנונימי זה ניתוק מיידי, '
                'עוד לפני שקורה משהו אחר.'),
        ],
    ),
    dict(
        slug='client-reconnects-immediately',
        title='בדפדפן: ניתוק מהשרת מוביל להתחברות מחדש מיד, בלי שום השהיה',
        sub='setupSocket ב-+layout.svelte',
        panels=[dict(file=LAY, ranges=[(179, 193), (268, 272), (281, 293)],
                     marks={181: 1, 187: 2, 191: 3, 286: 4}, hot=[182, 183, 184, 192, 193, 287])],
        notes=[
            (1, 'יש backoff של 1–5 שניות, אבל socket.io מפעיל אותו רק כשהחיבור נקטע ברשת. אחרי ניתוק יזום של '
                'השרת הוא לא מתחבר מחדש בעצמו, ולכן הקוד עושה את זה ידנית (סימן 4).'),
            (2, 'הטוקן נקרא מ-<code>localStorage</code> בכל התחברות. אם לשונית אחרת כבר מחקה אותו, הערך ריק '
                'והסוקט מתחבר כאנונימי (צילום 2).'),
            (3, '<code>connect_error</code> רק נכתב ל-console. אין יציאה מהמערכת ואין מעבר למסך ההתחברות.'),
            (4, 'אחרי <code>io server disconnect</code> (הניתוק מצילומים 3–4) הקוד קורא ל-<code>_socket.connect()</code> '
                '<b>מיד</b>. זה מה שסוגר את הלולאה.'),
        ],
    ),
    dict(
        slug='every-connect-refetches-sidebar',
        title='כל connect שולח מחדש גל של בקשות HTTP מהסרגל הצדדי',
        sub='Sidebar.svelte נרשם לאירוע connect פעמיים',
        panels=[dict(file=SIDE, ranges=[(483, 486), (533, 540), (773, 776), (840, 842)],
                     marks={775: 1, 842: 2, 534: 3, 536: 4}, hot=[484, 537])],
        notes=[
            (1, 'בכל connect נקראת <code>initChannels</code>, כלומר <code>GET /api/v1/channels/</code> (שורה 484).'),
            (2, 'ובכל connect נקראת גם <code>refreshChatRows</code>, ששולחת:'),
            (3, '<code>/api/v1/chats/</code> ו-<code>/api/v1/chats/pinned</code> במקביל.'),
            (4, 'רק אם שתיהן הצליחו נשלחות גם <code>/folders</code> ובקשה נוספת לכל תיקייה. עם טוקן שבוטל <code>getChatList</code> '
                'זורקת על ה-401, ולכן בקשות התיקיות לא נשלחות.'),
        ],
        callout='עם טוקן שבוטל, כל סבב שולח <code>chats</code>, <code>chats/pinned</code> ו-<code>channels</code>, ועם '
                'צ\'אט פתוח גם <code>tasks</code> (צילום 7). כולן מחזירות 401. זה בדיוק השילוב שמופיע אצל שלושת המשתמשים.',
    ),
    dict(
        slug='open-chat-join-closes-loop',
        title='צ\'אט פתוח: בכל connect נשלח join שהשרת עונה עליו בניתוק',
        sub='Chat.svelte, הלולאה של הלשוניות התקועות',
        panels=[
            dict(file=CHAT, ranges=[(1890, 1897), (1911, 1911)], marks={1894: 1, 1896: 2, 1911: 3}),
            dict(file=CHAT, ranges=[(1439, 1455)], marks={1449: 4}),
        ],
        notes=[
            (1, 'בכל connect, כשצ\'אט פתוח, הלשונית מצטרפת מחדש לחדר של הצ\'אט (שורה 1449).'),
            (2, 'וגם שולחת <code>GET /api/tasks/chat/&lt;id&gt;</code>. עם טוקן שבוטל: 401.'),
            (3, 'הפונקציה רשומה על כל אירוע <code>connect</code>, כולל כל התחברות מחדש.'),
            (4, 'ה-join מגיע לשרת כ-<code>events:chat</code>. סוקט אנונימי מנותק מיד (צילום 4, סימן 3), והדפדפן '
                'מתחבר מחדש מיד (צילום 5, סימן 4). כך נוצרת לולאה בלי שום השהיה, שה-rate limit רק מאט.'),
        ],
    ),
    dict(
        slug='no-redirect-to-login',
        title='למה 401 לא מוציא את הלשונית למסך ההתחברות',
        sub='עטיפת fetch ב-+layout.svelte והמעבר ל-/auth',
        panels=[
            dict(file=LAY, ranges=[(1151, 1166)], marks={1158: 1, 1160: 2}, hot=[1157, 1159, 1162]),
            dict(file=LAY, ranges=[(1025, 1036)], marks={1035: 3}),
            dict(file=LAY, ranges=[(1004, 1017)], marks={1014: 4}, hot=[1015]),
            dict(file=APPLAY, ranges=[(461, 463)], marks={461: 5}, hot=[462]),
        ],
        notes=[
            (1, 'הלשונית יוצאת רק אם יש טוקן ב-<code>localStorage</code>, שמשותף לכל הלשוניות '
                'של האתר. אחרי שלשונית אחת מחקה אותו (סימן 4), אצל כל השאר התנאי הזה לא יתקיים יותר לעולם.'),
            (2, 'ורק אם בקשת אישור ל-<code>/api/v1/auths/</code> מחזירה בדיוק 401.'),
            (3, 'בגלל <code>.catch(() =&gt; false)</code>, גם timeout או 503 בזמן עומס נחשבים "לא לצאת".'),
            (4, 'רק הלשונית שהגיעה לכאן מאפסת את <code>user</code> אצלה ומוחקת את הטוקן. אין ניתוק של הסוקט '
                'ואין הודעה ללשוניות האחרות.'),
            (5, 'המעבר ל-<code>/auth</code> קורה רק כש-<code>user</code> מתאפס, כלומר רק בלשונית של סימן 4. בשאר '
                'הלשוניות <code>user</code> נשאר, הן נשארות בצ\'אט ושולחות בקשות עם <code>Bearer undefined</code>.'),
        ],
    ),
    dict(
        slug='open-question-api-version',
        title='שאלה פתוחה: לפי הקוד, כל connect מתחיל בקריאה ל-/api/version',
        sub='ה-handler של connect ב-+layout.svelte',
        panels=[dict(file=LAY, ranges=[(197, 198), (213, 229), (252, 254)],
                     marks={214: 1, 223: 2, 254: 3}, hot=[226])],
        notes=[
            (1, 'לפני כל דבר אחר הלשונית קוראת ל-<code>/api/version</code>, שלא דורש אימות. לפי הקוד הקריאה הזו '
                'צריכה להופיע בכל סבב של הלולאה מצילומים 5–7.'),
            (2, 'אם <code>DEPLOYMENT_ID</code> השתנה, הלשונית נטענת מחדש ומקבלת JS חדש.'),
            (3, 'ה-<code>user-join</code> נשלח רק אחרי זה.'),
        ],
        callout='בשחזור הקריאה הזו מופיעה בכל סבב. בלוגים שלך היא לא מופיעה אצל שלושת ה-IP, ושינוי '
                '<code>DEPLOYMENT_ID</code> לא ריענן אותם. לכן או שהם לא בלולאה הזו, או שהבקשה לא מגיעה ללוג שבדקת. '
                'ספירת <code>/ws/socket.io</code> לכתובות שלהם ב-ingress תכריע.',
    ),
]
