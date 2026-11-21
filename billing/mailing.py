"""The email with the invoice and the hours report (step 3). Pure: it builds the message, it does not send."""

from email.message import EmailMessage

from django.template.loader import render_to_string


def invoice_filename(number):
    return f'Faktura {number.replace("/", "-")}.pdf'


def report_filename(work_month):
    return f'Raport godzin - {work_month.label}.pdf'


def subject_and_text(number, month, work_month, client, test):
    """The subject and the text, from the templates billing/email/subject.txt and body.txt."""
    context = {'number': number, 'month': month, 'work_month': work_month, 'client': client, 'test': test}
    subject = ' '.join(render_to_string('billing/email/subject.txt', context).split())
    text = render_to_string('billing/email/body.txt', context).strip() + '\n'
    return subject, text


def build_message(sender, recipients, subject, text, attachments):
    """A MIME message as bytes. attachments: [(file name, PDF bytes)]."""
    message = EmailMessage()
    message['From'] = sender
    message['To'] = ', '.join(recipients)
    message['Subject'] = subject
    message.set_content(text)
    for filename, data in attachments:
        message.add_attachment(data, maintype='application', subtype='pdf', filename=filename)
    return message.as_bytes()
