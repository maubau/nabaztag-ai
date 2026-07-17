/* ojn-plugin-events — webhook egress for Nabaztag click/RFID events.
 *
 * Rationale (nabaztag-ai Gate G0): the stock callurl plugin cannot deliver
 * events — its "CU <url>" packet reaches the rabbit but the OJN bootcode
 * never performs the HTTP request. Events must therefore leave server-side:
 * this plugin fires a GET to a per-bunny webhook URL on button clicks and
 * RFID reads. Licensed under the same terms as OpenJabNab (GPL v2, LICENSE).
 *
 * Enable per bunny:
 *   /ojn_api/bunny/<sn>/registerPlugin?name=events
 *   /ojn_api/bunny/<sn>/setSingleClickPlugin?name=events   (clicks only reach
 *   /ojn_api/bunny/<sn>/setDoubleClickPlugin?name=events    the click plugin)
 *   /ojn_api/bunny/<sn>/events/setWebhook?url=http://127.0.0.1:8091/event
 *
 * Webhook: GET <url>?bunny=<sn>&event=click&value=single|double
 *          GET <url>?bunny=<sn>&event=rfid&value=<tag hex>
 */
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QUrl>
#include "bunny.h"
#include "httprequest.h"
#include "log.h"
#include "plugin_events.h"

Q_EXPORT_PLUGIN2(plugin_events, PluginEvents)

PluginEvents::PluginEvents():PluginInterface("events", "Webhook egress for click/RFID events", BunnyPlugin)
{
	nam = new QNetworkAccessManager(this);
	connect(nam, SIGNAL(finished(QNetworkReply*)), this, SLOT(OnRequestFinished(QNetworkReply*)));
}

PluginEvents::~PluginEvents() {}

bool PluginEvents::Notify(Bunny * b, QString const& event, QString const& value)
{
	QString webhook = b->GetPluginSetting(GetName(), "Webhook", "").toString();
	if(webhook.isEmpty())
		return false;

	QUrl url(webhook);
	url.addQueryItem("bunny", QString(b->GetID()));
	url.addQueryItem("event", event);
	url.addQueryItem("value", value);
	nam->get(QNetworkRequest(url));
	return true;
}

bool PluginEvents::OnClick(Bunny * b, PluginInterface::ClickType type)
{
	return Notify(b, "click", type == PluginInterface::DoubleClick ? "double" : "single");
}

bool PluginEvents::OnRFID(Bunny * b, QByteArray const& tag)
{
	return Notify(b, "rfid", QString(tag.toHex()));
}

void PluginEvents::OnRequestFinished(QNetworkReply * reply)
{
	if(reply->error() != QNetworkReply::NoError)
		LogWarning(QString("events webhook failed: %1").arg(reply->errorString()));
	reply->deleteLater();
}

/*******/
/* API */
/*******/

void PluginEvents::InitApiCalls()
{
	DECLARE_PLUGIN_BUNNY_API_CALL("setWebhook(url)", PluginEvents, Api_SetWebhook);
	DECLARE_PLUGIN_BUNNY_API_CALL("getWebhook()", PluginEvents, Api_GetWebhook);
}

PLUGIN_BUNNY_API_CALL(PluginEvents::Api_SetWebhook)
{
	Q_UNUSED(account);

	if(!hRequest.HasArg("url"))
		return new ApiManager::ApiError(QString("Missing argument 'url' for plugin events"));

	bunny->SetPluginSetting(GetName(), "Webhook", hRequest.GetArg("url"));
	return new ApiManager::ApiOk(QString("Webhook set to '%1' for bunny '%2'").arg(hRequest.GetArg("url"), QString(bunny->GetID())));
}

PLUGIN_BUNNY_API_CALL(PluginEvents::Api_GetWebhook)
{
	Q_UNUSED(account);
	Q_UNUSED(hRequest);

	return new ApiManager::ApiString(bunny->GetPluginSetting(GetName(), "Webhook", "").toString());
}
