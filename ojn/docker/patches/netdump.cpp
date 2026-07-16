// nabaztag-ai security patch (replaces server/lib/netdump.cpp at image build).
//
// Upstream NetworkDump unconditionally appends every raw API URI — including
// pass=, token= and tk= query parameters — to dump.log in cleartext.
// This version:
//   1. is OFF by default: opt in with  [Log] NetworkDump = true  in openjabnab.ini
//      (GlobalSettings::Init() runs before NetworkDump::Init(), main.cpp order);
//   2. redacts pass/token/tk values even when enabled.
// dump.log stays in the container's ephemeral layer (applicationDirPath is not
// a /data symlink), so it dies with the container — by design.

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QRegExp>
#include <iostream>
#include "log.h"
#include "netdump.h"
#include "settings.h"

void NetworkDump::Init()
{
	if(!GlobalSettings::Get("Log/NetworkDump", false).toBool())
	{
		LogInfo("NetworkDump disabled (set Log/NetworkDump=true to enable; URIs are credential-redacted)");
		return;
	}
	// Open dump file
	QFile * dumpFile = new QFile(QDir(QCoreApplication::applicationDirPath()).absoluteFilePath("dump.log"));
	if(!dumpFile->open(QIODevice::Append))
	{
		LogError(QString("Error opening file : %1").arg(dumpFile->fileName()));
		return;
	}
	Instance().dumpStream.setDevice(dumpFile);
	Instance().dumpStream << QDateTime::currentDateTime().toString("dd/MM/yyyy hh:mm:ss") << " -- OpenJabNab Start --" << endl;
}

void NetworkDump::Close()
{
	QIODevice * d = Instance().dumpStream.device();
	if (!d)
		return;
	Instance().dumpStream << QDateTime::currentDateTime().toString("dd/MM/yyyy hh:mm:ss") << " -- OpenJabNab End --" << endl;
	d->close();
	delete d;
}

void NetworkDump::Log(QString const& what, QString const& txt)
{
	if(!Instance().dumpStream.device())
		return;
	QString clean(txt);
	clean.replace(QRegExp("(pass|token|tk)=[^&\\s]*"), "\\1=[REDACTED]");
	Instance().dumpStream << QDateTime::currentDateTime().toString("dd/MM/yyyy hh:mm:ss") << " - " << what << " - " << clean << endl;
}

NetworkDump & NetworkDump::Instance()
{
	static NetworkDump n;
	return n;
}
