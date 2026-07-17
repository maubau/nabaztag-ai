TEMPLATE = lib
CONFIG -= debug
CONFIG += plugin qt release
QT += network
QT -= gui
INCLUDEPATH += . ../../server ../../lib
TARGET = plugin_events
DESTDIR = ../../bin/plugins
DEPENDPATH += . ../../server ../../lib
LIBS += -L../../bin/ -lcommon
MOC_DIR = ./tmp/moc
OBJECTS_DIR = ./tmp/obj
unix {
	QMAKE_LFLAGS += -Wl,-rpath,\'\$$ORIGIN\'
}

# Input
HEADERS += plugin_events.h
SOURCES += plugin_events.cpp
