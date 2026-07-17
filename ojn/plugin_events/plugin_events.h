/* ojn-plugin-events — webhook egress for Nabaztag click/RFID events.
 *
 * Part of the nabaztag-ai project (github.com/maubau/nabaztag-ai), but this
 * plugin compiles against OpenJabNab and is licensed under the same terms as
 * OpenJabNab: GNU General Public License version 2 (see LICENSE, copied
 * verbatim from the OpenJabNab repository's COPYING).
 */
#ifndef _PLUGINEVENTS_H_
#define _PLUGINEVENTS_H_

#include <QNetworkAccessManager>
#include "plugininterface.h"

class QNetworkReply;

class PluginEvents : public PluginInterface
{
	Q_OBJECT
	Q_INTERFACES(PluginInterface)

public:
	PluginEvents();
	virtual ~PluginEvents();

	bool OnClick(Bunny *, PluginInterface::ClickType);
	bool OnRFID(Bunny *, QByteArray const&);

	// API
	void InitApiCalls();
	PLUGIN_BUNNY_API_CALL(Api_SetWebhook);
	PLUGIN_BUNNY_API_CALL(Api_GetWebhook);

private slots:
	void OnRequestFinished(QNetworkReply *);

private:
	bool Notify(Bunny *, QString const& event, QString const& value);
	QNetworkAccessManager * nam;
};

#endif
