"""The email with the invoice and the hours report (step 3). Pure: it builds the message, it does not send."""

from email.message import EmailMessage
from pathlib import Path

from django.template.loader import render_to_string

LOGO = Path(__file__).parent / 'templates' / 'billing' / 'email' / 'logo.jpg'
LOGO_CID = 'logo@noriet.pl'


def invoice_filename(number):
    return f'Faktura {number.replace("/", "-")}.pdf'


def report_filename(work_month):
    return f'Raport godzin - {work_month.label}.pdf'


def subject_and_body(number, month, work_month, client, test):
    """The subject, the plain text and the HTML, from the templates in billing/email/."""
    context = {'number': number, 'month': month, 'work_month': work_month, 'client': client, 'test': test,
               'logo_cid': LOGO_CID}
    subject = ' '.join(render_to_string('billing/email/subject.txt', context).split())
    text = render_to_string('billing/email/body.txt', context).strip() + '\n'
    html = render_to_string('billing/email/body.html', context)
    return subject, text, html


def build_message(sender, recipients, subject, text, html, attachments):
    """A MIME message as bytes: the text and the HTML (with the logo inline), and the PDF attachments.
    attachments: [(file name, PDF bytes)]."""
    message = EmailMessage()
    message['From'] = sender
    message['To'] = ', '.join(recipients)
    message['Subject'] = subject
    message.set_content(text)
    message.add_alternative(html, subtype='html')
    message.get_payload()[1].add_related(LOGO.read_bytes(), maintype='image', subtype='jpeg',
                                         cid=f'<{LOGO_CID}>', filename='noriet.jpg', disposition='inline')
    for filename, data in attachments:
        message.add_attachment(data, maintype='application', subtype='pdf', filename=filename)
    return message.as_bytes()
