from functools import partial

from PyQt5.QtCore import QUrl, QProcess
from PyQt5.QtWidgets import QLabel, QMessageBox

import config
import connectivity
from config import Settings
import chat
from model.player import Player
from model.playerset import Playerset
from client.connection import LobbyInfo, ServerConnection, \
        Dispatcher, ConnectionState, ServerReconnecter

from model.gameset import Gameset
from games.gamemodel import GameModel
from games.hostgamewidget import GameLauncher
from games.gameitem import GameViewBuilder

from client.updater import UpdateChecker, UpdateDialog, UpdateSettings
from client.update_settings import UpdateSettingsDialog
from client.theme_menu import ThemeMenu
from client.kick_dialog import KickDialog
from client.user import User
import fa
from connectivity.helper import ConnectivityHelper
from fa import GameSession
from fa.factions import Factions
from fa.maps import getUserMapsFolder
from modvault.utils import MODFOLDER
from ui.status_logo import StatusLogo
from client.login import LoginWidget
from ui.busy_widget import BusyWidget

from client.playercolors import PlayerColors
from client.gameannouncer import GameAnnouncer
'''
Created on Dec 1, 2011

@author: thygrrr
'''

from PyQt5 import QtCore, QtWidgets, QtGui

from client import ClientState, LOBBY_HOST, \
    LOBBY_PORT, LOCAL_REPLAY_PORT

import logging

logger = logging.getLogger(__name__)

import util
import secondaryServer

import json
import sys
import replays

import time
import notifications as ns

FormClass, BaseClass = util.THEME.loadUiType("client/client.ui")


class mousePosition(object):
    def __init__(self, parent):
        self.parent = parent
        self.onLeftEdge = False
        self.onRightEdge = False
        self.onTopEdge = False
        self.onBottomEdge = False
        self.cursorShapeChange = False
        self.warning_buttons = dict()
        self.onEdges = False

    def computeMousePosition(self, pos):
        self.onLeftEdge = pos.x() < 8
        self.onRightEdge = pos.x() > self.parent.size().width() - 8
        self.onTopEdge = pos.y() < 8
        self.onBottomEdge = pos.y() > self.parent.size().height() - 8

        self.onTopLeftEdge = self.onTopEdge and self.onLeftEdge
        self.onBottomLeftEdge = self.onBottomEdge and self.onLeftEdge
        self.onTopRightEdge = self.onTopEdge and self.onRightEdge
        self.onBottomRightEdge = self.onBottomEdge and self.onRightEdge

        self.onEdges = self.onLeftEdge or self.onRightEdge or self.onTopEdge or self.onBottomEdge

    def resetToFalse(self):
        self.onLeftEdge = False
        self.onRightEdge = False
        self.onTopEdge = False
        self.onBottomEdge = False
        self.cursorShapeChange = False

    def isOnEdge(self):
        return self.onEdges


class ClientWindow(FormClass, BaseClass):
    """
    This is the main lobby client that manages the FAF-related connection and data,
    in particular players, games, ranking, etc.
    Its UI also houses all the other UIs for the sub-modules.
    """

    state_changed = QtCore.pyqtSignal(object)
    authorized = QtCore.pyqtSignal(object)

    # These signals notify connected modules of game state changes (i.e. reasons why FA is launched)
    viewingReplay = QtCore.pyqtSignal(QtCore.QUrl)

    # Game state controls
    gameEnter = QtCore.pyqtSignal()
    gameExit = QtCore.pyqtSignal()

    # These signals propagate important client state changes to other modules
    localBroadcast = QtCore.pyqtSignal(str, str)
    autoJoin = QtCore.pyqtSignal(list)
    channelsUpdated = QtCore.pyqtSignal(list)

    matchmakerInfo = QtCore.pyqtSignal(dict)

    remember = Settings.persisted_property('user/remember', type=bool, default_value=True)
    login = Settings.persisted_property('user/login', persist_if=lambda self: self.remember)
    password = Settings.persisted_property('user/password', persist_if=lambda self: self.remember)

    gamelogs = Settings.persisted_property('game/logs', type=bool, default_value=True)
    useUPnP = Settings.persisted_property('game/upnp', type=bool, default_value=True)
    gamePort = Settings.persisted_property('game/port', type=int, default_value=6112)

    def __init__(self, *args, **kwargs):
        BaseClass.__init__(self, *args, **kwargs)

        logger.debug("Client instantiating")

        # Hook to Qt's application management system
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.cleanup)

        self.uniqueId = None

        self.sendFile = False
        self.warning_buttons = {}

        # Tray icon
        self.tray = QtWidgets.QSystemTrayIcon()
        self.tray.setIcon(util.THEME.icon("client/tray_icon.png"))
        self.tray.show()

        self._state = ClientState.NONE
        self.session = None

        # This dictates whether we login automatically in the beginning or
        # after a disconnect. We turn it on if we're sure we have correct
        # credentials and want to use them (if we were remembered or after
        # login) and turn it off if we're getting fresh credentials or
        # encounter a serious server error.
        self._autorelogin = self.remember

        self.lobby_dispatch = Dispatcher()
        self.lobby_connection = ServerConnection(LOBBY_HOST, LOBBY_PORT,
                                                 self.lobby_dispatch.dispatch)
        self.lobby_connection.state_changed.connect(self.on_connection_state_changed)
        self.lobby_reconnecter = ServerReconnecter(self.lobby_connection)

        self.players = Playerset()  # Players known to the client

        self.gameset = Gameset(self.players)
        fa.instance.gameset = self.gameset  # FIXME

        # Handy reference to the User object representing the logged-in user.
        self.me = User(self.players)

        # Qt model for displaying active games.
        self.game_model = GameModel(self.me, self.gameset)

        self.lobby_info = LobbyInfo(self.lobby_dispatch, self.gameset, self.players)
        self.gameset.newGame.connect(self.fill_in_session_info)

        self.lobby_dispatch["session"] = self.handle_session
        self.lobby_dispatch["registration_response"] = self.handle_registration_response
        self.lobby_dispatch["game_launch"] = self.handle_game_launch
        self.lobby_dispatch["matchmaker_info"] = self.handle_matchmaker_info
        self.lobby_dispatch["social"] = self.handle_social
        self.lobby_dispatch["player_info"] = self.handle_player_info
        self.lobby_dispatch["notice"] = self.handle_notice
        self.lobby_dispatch["invalid"] = self.handle_invalid
        self.lobby_dispatch["update"] = self.handle_update
        self.lobby_dispatch["welcome"] = self.handle_welcome
        self.lobby_dispatch["authentication_failed"] = self.handle_authentication_failed

        # Process used to run Forged Alliance (managed in module fa)
        fa.instance.started.connect(self.startedFA)
        fa.instance.finished.connect(self.finishedFA)
        fa.instance.error.connect(self.errorFA)
        self.gameset.newGame.connect(fa.instance.newServerGame)

        # Local Replay Server
        self.replayServer = fa.replayserver.ReplayServer(self)

        # GameSession
        self.game_session = None  # type: GameSession

        # ConnectivityTest
        self.connectivity = None  # type: ConnectivityHelper

        # stat server
        self.statsServer = secondaryServer.SecondaryServer("Statistic", 11002, self.lobby_dispatch)

        # create user interface (main window) and load theme
        self.setupUi(self)
        util.THEME.setStyleSheet(self, "client/client.css")

        self.setWindowTitle("FA Forever " + util.VERSION_STRING)

        # Frameless
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.CustomizeWindowHint)

        self.rubberBand = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Rectangle)

        self.mousePosition = mousePosition(self)
        self.installEventFilter(self)

        self.minimize = QtWidgets.QToolButton(self)
        self.minimize.setIcon(util.THEME.icon("client/minimize-button.png"))

        self.maximize = QtWidgets.QToolButton(self)
        self.maximize.setIcon(util.THEME.icon("client/maximize-button.png"))

        close = QtWidgets.QToolButton(self)
        close.setIcon(util.THEME.icon("client/close-button.png"))

        self.minimize.setMinimumHeight(10)
        close.setMinimumHeight(10)
        self.maximize.setMinimumHeight(10)

        close.setIconSize(QtCore.QSize(22, 22))
        self.minimize.setIconSize(QtCore.QSize(22, 22))
        self.maximize.setIconSize(QtCore.QSize(22, 22))

        close.setProperty("windowControlBtn", True)
        self.maximize.setProperty("windowControlBtn", True)
        self.minimize.setProperty("windowControlBtn", True)

        self.logo = StatusLogo(self)
        self.logo.disconnect_requested.connect(self.disconnect)
        self.logo.reconnect_requested.connect(self.reconnect)
        self.logo.about_dialog_requested.connect(self.linkAbout)
        self.logo.connectivity_dialog_requested.connect(self.connectivityDialog)

        self.menu = self.menuBar()
        self.topLayout.addWidget(self.logo)
        titleLabel = QLabel("FA Forever" if not config.is_beta() else "FA Forever BETA")
        titleLabel.setProperty('titleLabel', True)
        self.topLayout.addWidget(titleLabel)
        self.topLayout.addStretch(500)
        self.topLayout.addWidget(self.menu)
        self.topLayout.addWidget(self.minimize)
        self.topLayout.addWidget(self.maximize)
        self.topLayout.addWidget(close)
        self.topLayout.setSpacing(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.maxNormal = False

        close.clicked.connect(self.close)
        self.minimize.clicked.connect(self.showSmall)
        self.maximize.clicked.connect(self.showMaxRestore)

        self.moving = False
        self.dragging = False
        self.draggingHover = False
        self.offset = None
        self.curSize = None

        sizeGrip = QtWidgets.QSizeGrip(self)
        self.mainGridLayout.addWidget(sizeGrip, 2, 2)

        # Wire all important signals
        self._main_tab = -1
        self.mainTabs.currentChanged.connect(self.mainTabChanged)
        self._vault_tab = -1
        self.topTabs.currentChanged.connect(self.vaultTabChanged)

        self.player_colors = PlayerColors(self.me)

        self.game_announcer = GameAnnouncer(self.gameset, self.me,
                                            self.player_colors, self)

        self.power = 0  # current user power
        self.id = 0
        # Initialize the Menu Bar according to settings etc.
        self.initMenus()

        # Load the icons for the tabs
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.whatNewTab), util.THEME.icon("client/feed.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.chatTab), util.THEME.icon("client/chat.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.gamesTab), util.THEME.icon("client/games.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.coopTab), util.THEME.icon("client/coop.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.vaultsTab), util.THEME.icon("client/mods.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.ladderTab), util.THEME.icon("client/ladder.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.tourneyTab), util.THEME.icon("client/tourney.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.unitdbTab), util.THEME.icon("client/twitch.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.replaysTab), util.THEME.icon("client/replays.png"))
        self.mainTabs.setTabIcon(self.mainTabs.indexOf(self.tutorialsTab), util.THEME.icon("client/tutorials.png"))

        # for moderator
        self.modMenu = None

        #self.nFrame = NewsFrame()
        #self.whatsNewLayout.addWidget(self.nFrame)
        #self.nFrame.collapse()

        #self.nFrame = NewsFrame()
        #self.whatsNewLayout.addWidget(self.nFrame)

        #self.nFrame = NewsFrame()
        #self.whatsNewLayout.addWidget(self.nFrame)


        #self.WPApi = WPAPI(self)
        #self.WPApi.newsDone.connect(self.on_wpapi_done)
        #self.WPApi.download()

        #self.controlsContainerLayout.setAlignment(self.pageControlFrame, QtCore.Qt.AlignRight)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value
        self.state_changed.emit(value)

    def on_connection_state_changed(self, state):
        if self.state == ClientState.SHUTDOWN:
            return

        if state == ConnectionState.CONNECTED:
            self.on_connected()
            self.state = ClientState.CONNECTED
        elif state == ConnectionState.DISCONNECTED:
            self.on_disconnected()
            self.state = ClientState.DISCONNECTED
        elif state == ConnectionState.CONNECTING:
            self.state = ClientState.CONNECTING

    def on_connected(self):
        # Enable reconnect in case we used to explicitly stay offline
        self.lobby_reconnecter.enabled = True

        self.lobby_connection.send(dict(command="ask_session",
                                        version=config.VERSION,
                                        user_agent="faf-client"))

    def on_disconnected(self):
        logger.warning("Disconnected from lobby server.")
        self.gameset.clear()
        self.clear_players()

    @QtCore.pyqtSlot(bool)
    def on_actionSavegamelogs_toggled(self, value):
        self.gamelogs = value

    @QtCore.pyqtSlot(bool)
    def on_actionAutoDownloadMods_toggled(self, value):
        Settings.set('mods/autodownload', value is True)

    @QtCore.pyqtSlot(bool)
    def on_actionAutoDownloadMaps_toggled(self, value):
        Settings.set('maps/autodownload', value is True)

    def eventFilter(self, obj, event):
        if (event.type() == QtCore.QEvent.HoverMove):
            self.draggingHover = self.dragging
            if self.dragging:
                self.resizeWidget(self.mapToGlobal(event.pos()))
            else:
                if self.maxNormal == False:
                    self.mousePosition.computeMousePosition(event.pos())
                else:
                    self.mousePosition.resetToFalse()
            self.updateCursorShape(event.pos())

        return False

    def updateCursorShape(self, pos):
        if self.mousePosition.onTopLeftEdge or self.mousePosition.onBottomRightEdge:
            self.mousePosition.cursorShapeChange = True
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        elif self.mousePosition.onTopRightEdge or self.mousePosition.onBottomLeftEdge:
            self.setCursor(QtCore.Qt.SizeBDiagCursor)
            self.mousePosition.cursorShapeChange = True
        elif self.mousePosition.onLeftEdge or self.mousePosition.onRightEdge:
            self.setCursor(QtCore.Qt.SizeHorCursor)
            self.mousePosition.cursorShapeChange = True
        elif self.mousePosition.onTopEdge or self.mousePosition.onBottomEdge:
            self.setCursor(QtCore.Qt.SizeVerCursor)
            self.mousePosition.cursorShapeChange = True
        else:
            if self.mousePosition.cursorShapeChange == True:
                self.unsetCursor()
                self.mousePosition.cursorShapeChange = False

    def showSmall(self):
        self.showMinimized()

    def showMaxRestore(self):
        if (self.maxNormal):
            self.maxNormal = False
            if self.curSize:
                self.setGeometry(self.curSize)

        else:
            self.maxNormal = True
            self.curSize = self.geometry()
            self.setGeometry(QtWidgets.QDesktopWidget().availableGeometry(self))

    def mouseDoubleClickEvent(self, event):
        self.showMaxRestore()

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.moving = False
        if self.rubberBand.isVisible():
            self.maxNormal = True
            self.curSize = self.geometry()
            self.setGeometry(self.rubberBand.geometry())
            self.rubberBand.hide()
            # self.showMaxRestore()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            if self.mousePosition.isOnEdge() and self.maxNormal == False:
                self.dragging = True
                return
            else:
                self.dragging = False

            self.moving = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.dragging and self.draggingHover == False:
            self.resizeWidget(event.globalPos())

        elif self.moving and self.offset is not None:
            desktop = QtWidgets.QDesktopWidget().availableGeometry(self)
            if event.globalPos().y() == 0:
                self.rubberBand.setGeometry(desktop)
                self.rubberBand.show()
            elif event.globalPos().x() == 0:
                desktop.setRight(desktop.right() / 2.0)
                self.rubberBand.setGeometry(desktop)
                self.rubberBand.show()
            elif event.globalPos().x() == desktop.right():
                desktop.setRight(desktop.right() / 2.0)
                desktop.moveLeft(desktop.right())
                self.rubberBand.setGeometry(desktop)
                self.rubberBand.show()

            else:
                self.rubberBand.hide()
                if self.maxNormal == True:
                    self.showMaxRestore()

            self.move(event.globalPos() - self.offset)

    def resizeWidget(self, globalMousePos):
        if globalMousePos.y() == 0:
            self.rubberBand.setGeometry(QtWidgets.QDesktopWidget().availableGeometry(self))
            self.rubberBand.show()
        else:
            self.rubberBand.hide()

        origRect = self.frameGeometry()

        left, top, right, bottom = origRect.getCoords()
        minWidth = self.minimumWidth()
        minHeight = self.minimumHeight()
        if self.mousePosition.onTopLeftEdge:
            left = globalMousePos.x()
            top = globalMousePos.y()

        elif self.mousePosition.onBottomLeftEdge:
            left = globalMousePos.x()
            bottom = globalMousePos.y()
        elif self.mousePosition.onTopRightEdge:
            right = globalMousePos.x()
            top = globalMousePos.y()
        elif self.mousePosition.onBottomRightEdge:
            right = globalMousePos.x()
            bottom = globalMousePos.y()
        elif self.mousePosition.onLeftEdge:
            left = globalMousePos.x()
        elif self.mousePosition.onRightEdge:
            right = globalMousePos.x()
        elif self.mousePosition.onTopEdge:
            top = globalMousePos.y()
        elif self.mousePosition.onBottomEdge:
            bottom = globalMousePos.y()

        newRect = QtCore.QRect(QtCore.QPoint(left, top), QtCore.QPoint(right, bottom))
        if newRect.isValid():
            if minWidth > newRect.width():
                if left != origRect.left():
                    newRect.setLeft(origRect.left())
                else:
                    newRect.setRight(origRect.right())
            if minHeight > newRect.height():
                if top != origRect.top():
                    newRect.setTop(origRect.top())
                else:
                    newRect.setBottom(origRect.bottom())

            self.setGeometry(newRect)

    def setup(self):
        import chat
        import tourneys
        import stats
        import vault
        import games
        import tutorials
        import downloadManager
        import modvault
        import coop
        import news
        from chat._avatarWidget import AvatarWidget

        # download manager
        self.downloader = downloadManager.downloadManager(self)

        self.loadSettings()

        # Initialize chat
        self.chat = chat.ChatWidget(self, self.players, self.me)

        self.gameview_builder = GameViewBuilder(self.me,
                                                self.player_colors,
                                                self.downloader)
        self.game_launcher = GameLauncher(self.players, self.me,
                                          self, self.gameview_builder)

        # build main window with the now active client
        self.news = news.NewsWidget(self)
        self.ladder = stats.Stats(self)
        self.games = games.Games(self, self.game_model, self.me,
                                 self.gameview_builder, self.game_launcher)
        self.tourneys = tourneys.Tourneys(self)
        self.vault = vault.MapVault(self)
        self.modvault = modvault.ModVault(self)
        self.replays = replays.Replays(self, self.lobby_dispatch,
                                       self.gameset, self.players)
        self.tutorials = tutorials.Tutorials(self)
        self.Coop = coop.Coop(self, self.game_model, self.me,
                              self.gameview_builder, self.game_launcher)
        self.notificationSystem = ns.Notifications(self, self.gameset,
                                                   self.players, self.me)

        # TODO: some day when the tabs only do UI we'll have all this in the .ui file
        self.chatTab.layout().addWidget(self.chat)
        self.whatNewTab.layout().addWidget(self.news)
        self.ladderTab.layout().addWidget(self.ladder)
        self.gamesTab.layout().addWidget(self.games)
        self.tourneyTab.layout().addWidget(self.tourneys)
        self.mapsTab.layout().addWidget(self.vault.ui)
        self.modsTab.layout().addWidget(self.modvault)
        self.replaysTab.layout().addWidget(self.replays)
        self.tutorialsTab.layout().addWidget(self.tutorials)
        self.coopTab.layout().addWidget(self.Coop)

        # set menu states
        self.actionNsEnabled.setChecked(self.notificationSystem.settings.enabled)

        # Other windows
        self.avatarAdmin = self.avatarSelection = AvatarWidget(self, None)

        # warning setup
        self.warning = QtWidgets.QHBoxLayout()

        # units database (ex. live streams)
        # old unitDB
        self.unitdbWebView.setUrl(QtCore.QUrl("http://direct.faforever.com/faf/unitsDB"))
        # spookys unitDB (will be moved to site)
        # self.unitdbWebView.setUrl(QtCore.QUrl("http://spooky.github.io/unitdb/#/"))

        self.warnPlayer = QtWidgets.QLabel(self)
        self.warnPlayer.setText(
            "A player of your skill level is currently searching for a 1v1 game. Click a faction to join them! ")
        self.warnPlayer.setAlignment(QtCore.Qt.AlignHCenter)
        self.warnPlayer.setAlignment(QtCore.Qt.AlignVCenter)
        self.warnPlayer.setProperty("warning", True)
        self.warning.addStretch()
        self.warning.addWidget(self.warnPlayer)

        def add_warning_button(faction):
            button = QtWidgets.QToolButton(self)
            button.setMaximumSize(25, 25)
            button.setIcon(util.THEME.icon("games/automatch/%s.png" % faction.to_name()))
            button.clicked.connect(partial(self.games.startSearchRanked, faction))
            self.warning.addWidget(button)
            return button

        self.warning_buttons = {faction: add_warning_button(faction) for faction in Factions}

        self.warning.addStretch()

        self.mainGridLayout.addLayout(self.warning, 2, 0)
        self.warningHide()

        self._update_checker = UpdateChecker(self)
        self._update_checker.finished.connect(self.update_checked)
        self._update_checker.start()

    def warningHide(self):
        """
        hide the warning bar for matchmaker
        """
        self.warnPlayer.hide()
        for i in list(self.warning_buttons.values()):
            i.hide()

    def warningShow(self):
        """
        show the warning bar for matchmaker
        """
        self.warnPlayer.show()
        for i in list(self.warning_buttons.values()):
            i.show()

    def reconnect(self):
        self._update_checker.start()

        self.lobby_reconnecter.enabled = True
        self.lobby_connection.doConnect()

    def disconnect(self):
        # Used when the user explicitly demanded to stay offline.
        self.lobby_reconnecter.enabled = False
        self.lobby_connection.disconnect()
        self.chat.disconnect()

    @QtCore.pyqtSlot(list)
    def update_checked(self, releases):
        if len(releases) > 0:
            update_dialog = UpdateDialog(self)
            update_dialog.setup(releases)
            update_dialog.show()
        else:
            QMessageBox.information(self,"No updates found", "No client updates were found")

    @QtCore.pyqtSlot()
    def cleanup(self):
        """
        Perform cleanup before the UI closes
        """
        self.state = ClientState.SHUTDOWN

        progress = QtWidgets.QProgressDialog()
        progress.setMinimum(0)
        progress.setMaximum(0)
        progress.setWindowTitle("FAF is shutting down")
        progress.setMinimum(0)
        progress.setMaximum(0)
        progress.setValue(0)
        progress.setCancelButton(None)
        progress.show()

        # Important: If a game is running, offer to terminate it gently
        progress.setLabelText("Closing ForgedAllianceForever.exe")
        if fa.instance.running():
            fa.instance.close()

        # Terminate Lobby Server connection
        self.lobby_reconnecter.enabled = False
        if self.lobby_connection.socket_connected():
            progress.setLabelText("Closing main connection.")
            self.lobby_connection.disconnect()

        # Clear UPnP Mappings...
        if self.useUPnP:
            progress.setLabelText("Removing UPnP port mappings")
            fa.upnp.removePortMappings()

        # Terminate local ReplayServer
        if self.replayServer:
            progress.setLabelText("Terminating local replay server")
            self.replayServer.close()
            self.replayServer = None

        # Clean up Chat
        if self.chat:
            progress.setLabelText("Disconnecting from IRC")
            self.chat.disconnect()
            self.chat = None

        # Get rid of the Tray icon
        if self.tray:
            progress.setLabelText("Removing System Tray icon")
            self.tray.deleteLater()
            self.tray = None

        # Terminate UI
        if self.isVisible():
            progress.setLabelText("Closing main window")
            self.close()

        progress.close()

    def closeEvent(self, event):
        logger.info("Close Event for Application Main Window")
        self.saveWindow()

        if fa.instance.running():
            if QtWidgets.QMessageBox.question(self, "Are you sure?", "Seems like you still have Forged Alliance "
                                                                     "running!<br/><b>Close anyway?</b>",
                                              QtWidgets.QMessageBox.Yes,
                                              QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.No:
                event.ignore()
                return

        return QtWidgets.QMainWindow.closeEvent(self, event)

    def initMenus(self):
        self.actionCheck_for_Updates.triggered.connect(self.check_for_updates)
        self.actionUpdate_Settings.triggered.connect(self.show_update_settings)
        self.actionLink_account_to_Steam.triggered.connect(partial(self.open_url, Settings.get("STEAMLINK_URL")))
        self.actionLinkWebsite.triggered.connect(partial(self.open_url, Settings.get("WEBSITE_URL")))
        self.actionLinkWiki.triggered.connect(partial(self.open_url, Settings.get("WIKI_URL")))
        self.actionLinkForums.triggered.connect(partial(self.open_url, Settings.get("FORUMS_URL")))
        self.actionLinkUnitDB.triggered.connect(partial(self.open_url, Settings.get("UNITDB_URL")))
        self.actionLinkGitHub.triggered.connect(partial(self.open_url, Settings.get("GITHUB_URL")))

        self.actionNsSettings.triggered.connect(lambda: self.notificationSystem.on_showSettings())
        self.actionNsEnabled.triggered.connect(lambda enabled: self.notificationSystem.setNotificationEnabled(enabled))

        self.actionWiki.triggered.connect(partial(self.open_url, Settings.get("WIKI_URL")))
        self.actionReportBug.triggered.connect(partial(self.open_url, Settings.get("TICKET_URL")))
        self.actionShowLogs.triggered.connect(self.linkShowLogs)
        self.actionTechSupport.triggered.connect(partial(self.open_url, Settings.get("SUPPORT_URL")))
        self.actionAbout.triggered.connect(self.linkAbout)

        self.actionClearCache.triggered.connect(self.clearCache)
        self.actionClearSettings.triggered.connect(self.clearSettings)
        self.actionClearGameFiles.triggered.connect(self.clearGameFiles)

        self.actionSetGamePath.triggered.connect(self.switchPath)
        self.actionSetGamePort.triggered.connect(self.switchPort)

        self.actionShowMapsDir.triggered.connect(lambda: util.showDirInFileBrowser(getUserMapsFolder()))
        self.actionShowModsDir.triggered.connect(lambda: util.showDirInFileBrowser(MODFOLDER))
        self.actionShowReplaysDir.triggered.connect(lambda: util.showDirInFileBrowser(util.REPLAY_DIR))
        self.actionShowThemesDir.triggered.connect(lambda: util.showDirInFileBrowser(util.THEME_DIR))
        # if game.prefs doesn't exist: show_dir -> empty folder / show_file -> 'file doesn't exist' message
        self.actionShowGamePrefs.triggered.connect(lambda: util.showDirInFileBrowser(util.LOCALFOLDER))
        #self.actionShowGamePrefs.triggered.connect(lambda: util.showFileInFileBrowser(util.PREFSFILENAME))

        # Toggle-Options
        self.actionSetAutoLogin.triggered.connect(self.updateOptions)
        self.actionSetAutoLogin.setChecked(self.remember)
        self.actionSetAutoDownloadMods.toggled.connect(self.on_actionAutoDownloadMods_toggled)
        self.actionSetAutoDownloadMods.setChecked(Settings.get('mods/autodownload', type=bool, default=False))
        self.actionSetAutoDownloadMaps.toggled.connect(self.on_actionAutoDownloadMaps_toggled)
        self.actionSetAutoDownloadMaps.setChecked(Settings.get('maps/autodownload', type=bool, default=False))
        self.actionSetSoundEffects.triggered.connect(self.updateOptions)
        self.actionSetOpenGames.triggered.connect(self.updateOptions)
        self.actionSetJoinsParts.triggered.connect(self.updateOptions)
        self.actionSetLiveReplays.triggered.connect(self.updateOptions)
        self.actionSaveGamelogs.toggled.connect(self.on_actionSavegamelogs_toggled)
        self.actionSaveGamelogs.setChecked(self.gamelogs)
        self.actionColoredNicknames.triggered.connect(self.updateOptions)
        self.actionFriendsOnTop.triggered.connect(self.updateOptions)

        self._menuThemeHandler = ThemeMenu(self.menuTheme)
        self._menuThemeHandler.setup(util.THEME.listThemes())
        self._menuThemeHandler.themeSelected.connect(lambda theme: util.THEME.setTheme(theme, True))

    @QtCore.pyqtSlot()
    def updateOptions(self):
        self.remember = self.actionSetAutoLogin.isChecked()
        self.soundeffects = self.actionSetSoundEffects.isChecked()
        self.game_announcer.announce_games = self.actionSetOpenGames.isChecked()
        self.joinsparts = self.actionSetJoinsParts.isChecked()
        self.game_announcer.announce_replays = self.actionSetLiveReplays.isChecked()

        self.gamelogs = self.actionSaveGamelogs.isChecked()
        self.player_colors.coloredNicknames = self.actionColoredNicknames.isChecked()
        self.friendsontop = self.actionFriendsOnTop.isChecked()

        self.saveChat()

    @QtCore.pyqtSlot()
    def switchPath(self):
        fa.wizards.Wizard(self).exec_()

    @QtCore.pyqtSlot()
    def switchPort(self):
        from . import loginwizards
        loginwizards.gameSettingsWizard(self).exec_()

    @QtCore.pyqtSlot()
    def clearSettings(self):
        result = QtWidgets.QMessageBox.question(None, "Clear Settings", "Are you sure you wish to clear all settings, "
                                                                        "login info, etc. used by this program?",
                                                QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No)
        if result == QtWidgets.QMessageBox.Yes:
            util.settings.clear()
            util.settings.sync()
            QtWidgets.QMessageBox.information(None, "Restart Needed", "FAF will quit now.")
            QtWidgets.QApplication.quit()

    @QtCore.pyqtSlot()
    def clearGameFiles(self):
        util.clearDirectory(util.BIN_DIR)
        util.clearDirectory(util.GAMEDATA_DIR)

    @QtCore.pyqtSlot()
    def clearCache(self):
        changed = util.clearDirectory(util.CACHE_DIR)
        if changed:
            QtWidgets.QMessageBox.information(None, "Restart Needed", "FAF will quit now.")
            QtWidgets.QApplication.quit()

    # Clear the online users lists
    def clear_players(self):
        self.players.clear()

    @QtCore.pyqtSlot(str)
    def open_url(self, url):
        QtGui.QDesktopServices.openUrl(QUrl(url))

    @QtCore.pyqtSlot()
    def linkShowLogs(self):
        util.showDirInFileBrowser(util.LOG_DIR)

    @QtCore.pyqtSlot()
    def connectivityDialog(self):
        dialog = connectivity.ConnectivityDialog(self.connectivity)
        dialog.exec_()

    @QtCore.pyqtSlot()
    def linkAbout(self):
        dialog = util.THEME.loadUi("client/about.ui")
        dialog.version_label.setText("Version: {}".format(util.VERSION_STRING))
        dialog.exec_()

    @QtCore.pyqtSlot()
    def check_for_updates(self):
        self._update_checker.respect_notify = False
        self._update_checker.start(reset_server=False)

    @QtCore.pyqtSlot()
    def show_update_settings(self):
        dialog = UpdateSettingsDialog(self)
        dialog.setup()
        dialog.show()

    def saveWindow(self):
        util.settings.beginGroup("window")
        util.settings.setValue("geometry", self.saveGeometry())
        util.settings.endGroup()

    def saveChat(self):
        util.settings.beginGroup("chat")
        util.settings.setValue("soundeffects", self.soundeffects)
        util.settings.setValue("livereplays", self.game_announcer.announce_replays)
        util.settings.setValue("opengames", self.game_announcer.announce_games)
        util.settings.setValue("joinsparts", self.joinsparts)
        util.settings.setValue("coloredNicknames", self.player_colors.coloredNicknames)
        util.settings.setValue("friendsontop", self.friendsontop)
        util.settings.endGroup()

    def loadSettings(self):
        self.loadChat()
        # Load settings
        util.settings.beginGroup("window")
        geometry = util.settings.value("geometry", None)
        if geometry:
            self.restoreGeometry(geometry)
        util.settings.endGroup()

        util.settings.beginGroup("ForgedAlliance")
        util.settings.endGroup()

    def loadChat(self):
        try:
            util.settings.beginGroup("chat")
            self.soundeffects = (util.settings.value("soundeffects", "true") == "true")
            self.game_announcer.announce_games = (util.settings.value("opengames", "true") == "true")
            self.joinsparts = (util.settings.value("joinsparts", "false") == "true")
            self.game_announcer.announce_replays = (util.settings.value("livereplays", "true") == "true")
            self.player_colors.coloredNicknames = (util.settings.value("coloredNicknames", "false") == "true")
            self.friendsontop = (util.settings.value("friendsontop", "false") == "true")

            util.settings.endGroup()
            self.actionColoredNicknames.setChecked(self.player_colors.coloredNicknames)
            self.actionFriendsOnTop.setChecked(self.friendsontop)
            self.actionSetSoundEffects.setChecked(self.soundeffects)
            self.actionSetLiveReplays.setChecked(self.game_announcer.announce_replays)
            self.actionSetOpenGames.setChecked(self.game_announcer.announce_games)
            self.actionSetJoinsParts.setChecked(self.joinsparts)
        except:
            pass

    def doConnect(self):
        if not self.replayServer.doListen(LOCAL_REPLAY_PORT):
            return False

        self.lobby_connection.doConnect()
        return True

    def set_remember(self, remember):
        self.remember = remember
        self.actionSetAutoLogin.setChecked(self.remember)  # FIXME - option updating is silly

    def get_creds_and_login(self):
        # Try to autologin, or show login widget if we fail or can't do that.
        if self._autorelogin and self.password and self.login:
            if self.send_login(self.login, self.password):
                return

        self.show_login_widget()

    def show_login_widget(self):
        login_widget = LoginWidget(self.login, self.remember)
        login_widget.finished.connect(self.on_widget_login_data)
        login_widget.rejected.connect(self.on_widget_no_login)
        login_widget.request_quit.connect(self.on_login_widget_quit)
        login_widget.remember.connect(self.set_remember)
        login_widget.exec_()

    def on_widget_login_data(self, login, password):
        self.login = login
        self.password = password

        if self.send_login(login, password):
            return
        self.show_login_widget()

    def on_widget_no_login(self):
        self.disconnect()

    def on_login_widget_quit(self):
        QtWidgets.QApplication.quit()

    def send_login(self, login, password):
        # Send login data once we have the creds.
        self._autorelogin = False # Fresh credentials
        if config.is_beta():    # Replace for develop here to not clobber the real pass
            password = util.password_hash("foo")
        self.uniqueId = util.uniqueID(self.login, self.session)
        if not self.uniqueId:
            QtWidgets.QMessageBox.critical(self,
                                           "Failed to calculate UID",
                                           "Failed to calculate your unique ID"
                                           " (a part of our smurf prevention system).</br>"
                                           "Please report this to the tech support forum!")
            return False
        self.lobby_connection.send(dict(command="hello",
                                        login=login,
                                        password=password,
                                        unique_id=self.uniqueId,
                                        session=self.session))
        return True

    @QtCore.pyqtSlot()
    def startedFA(self):
        """
        Slot hooked up to fa.instance when the process has launched.
        It will notify other modules through the signal gameEnter().
        """
        logger.info("FA has launched in an attached process.")
        self.gameEnter.emit()

    @QtCore.pyqtSlot(int)
    def finishedFA(self, exit_code):
        """
        Slot hooked up to fa.instance when the process has ended.
        It will notify other modules through the signal gameExit().
        """
        if not exit_code:
            logger.info("FA has finished with exit code: " + str(exit_code))
        else:
            logger.warning("FA has finished with exit code: " + str(exit_code))
        self.gameExit.emit()

    @QtCore.pyqtSlot(QProcess.ProcessError)
    def errorFA(self, error_code):
        """
        Slot hooked up to fa.instance when the process has failed to start.
        """
        logger.error("FA has died with error: " + fa.instance.errorString())
        if error_code == 0:
            logger.error("FA has failed to start")
            QtWidgets.QMessageBox.critical(self, "Error from FA", "FA has failed to start.")
        elif error_code == 1:
            logger.error("FA has crashed or killed after starting")
        else:
            text = "FA has failed to start with error code: " + str(error_code)
            logger.error(text)
            QtWidgets.QMessageBox.critical(self, "Error from FA", text)
        self.gameExit.emit()

    def _tabChanged(self, tab, curr, prev):
        """
        The main visible tab (module) of the client's UI has changed.
        In this case, other modules may want to load some data or cease
        particularly CPU-intensive interactive functionality.
        """
        new_tab = tab.widget(curr)
        old_tab = tab.widget(prev)

        if old_tab is not None:
            tab = old_tab.layout().itemAt(0).widget()
            if isinstance(tab, BusyWidget):
                tab.busy_left()
        if new_tab is not None:
            tab = new_tab.layout().itemAt(0).widget()
            if isinstance(tab, BusyWidget):
                tab.busy_entered()

    @QtCore.pyqtSlot(int)
    def mainTabChanged(self, curr):
        self._tabChanged(self.mainTabs, curr, self._main_tab)
        self._main_tab = curr

    @QtCore.pyqtSlot(int)
    def vaultTabChanged(self, curr):
        self._tabChanged(self.topTabs, curr, self._vault_tab)
        self._vault_tab = curr

    @QtCore.pyqtSlot()
    def joinGameFromURL(self, url):
        """
        Tries to join the game at the given URL
        """
        logger.debug("joinGameFromURL: " + url.toString())
        if fa.instance.available():
            add_mods = []
            try:
                modstr = QtCore.QUrlQuery(url).queryItemValue("mods")
                add_mods = json.loads(modstr)  # should be a list
            except:
                logger.info("Couldn't load urlquery value 'mods'")
            if fa.check.game(self):
                uid, mod, map = QtCore.QUrlQuery(url).queryItemValue('uid'), \
                                QtCore.QUrlQuery(url).queryItemValue('mod'), \
                                QtCore.QUrlQuery(url).queryItemValue('map')
                if fa.check.check(mod, map, sim_mods=add_mods):
                    self.join_game(int(uid))

    @QtCore.pyqtSlot()
    def forwardLocalBroadcast(self, source, message):
        self.localBroadcast.emit(source, message)

    def manage_power(self):
        """ update the interface accordingly to the power of the user """
        if self.power >= 1:
            if self.modMenu is None:
                self.modMenu = self.menu.addMenu("Administration")

            actionAvatar = QtWidgets.QAction("Avatar manager", self.modMenu)
            actionAvatar.triggered.connect(self.avatarManager)
            self.modMenu.addAction(actionAvatar)

            self.modMenu.addSeparator()

            actionLobbyKick = QtWidgets.QAction("Close player's FAF Client...", self.modMenu)
            actionLobbyKick.triggered.connect(lambda: self.closeLobby())
            self.modMenu.addAction(actionLobbyKick)

            actionCloseFA = QtWidgets.QAction("Close Player's Game...", self.modMenu)
            actionCloseFA.triggered.connect(lambda: util.userNameAction(self, 'Player to close FA (do not typo!)',
                                                                        lambda name: self.closeFA(name)))
            self.modMenu.addAction(actionCloseFA)

    def requestAvatars(self, personal):
        if personal:
            self.lobby_connection.send(dict(command="avatar", action="list_avatar"))
        else:
            self.lobby_connection.send(dict(command="admin", action="requestavatars"))

    def joinChannel(self, username, channel):
        """ Join users to a channel """
        self.lobby_connection.send(dict(command="admin", action="join_channel",
                                        user_ids=[self.players.getID(username)], channel=channel))

    def closeFA(self, username):
        """ Close FA remotely """
        logger.info('closeFA for {}'.format(username))
        user_id = self.players.getID(username)
        if user_id != -1:
            self.lobby_connection.send(dict(command="admin", action="closeFA", user_id=user_id))

    def closeLobby(self, username=""):
        """ Close lobby remotely """
        logger.info('Opening kick dialog for {}'.format(username))
        kick_dialog = KickDialog(self)
        kick_dialog.reset(username)
        kick_dialog.show()

    def addFriend(self, friend_id):
        if friend_id in self.players:
            self.me.addFriend(int(friend_id))
            self.lobby_connection.send(dict(command="social_add", friend=friend_id))

    def addFoe(self, foe_id):
        if foe_id in self.players:
            self.me.addFoe(int(foe_id))
            self.lobby_connection.send(dict(command="social_add", foe=foe_id))

    def remFriend(self, friend_id):
        if friend_id in self.players:
            self.me.remFriend(int(friend_id))
            self.lobby_connection.send(dict(command="social_remove", friend=friend_id))

    def remFoe(self, foe_id):
        if foe_id in self.players:
            self.me.remFoe(int(foe_id))
            self.lobby_connection.send(dict(command="social_remove", foe=foe_id))

    def handle_session(self, message):
        self._update_checker.server_session()

        self.session = str(message['session'])
        self.get_creds_and_login()

    def handle_update(self, message):
        # Remove geometry settings prior to updating
        # could be incompatible with an updated client.
        Settings.remove('window/geometry')

        logger.warning("Server says we need an update")
        self._update_checker.server_update(message)

    def handle_welcome(self, message):
        self.state = ClientState.LOGGED_IN
        self._autorelogin = True
        self.id = message["id"]
        self.login = message["login"]

        self.me.player = Player(id_=self.id, login=self.login)

        self.players[self.me.player.id] = self.me.player  # FIXME
        logger.debug("Login success")

        util.crash.CRASH_REPORT_USER = self.login

        if self.useUPnP:
            self.lobby_connection.set_upnp(self.gamePort)

        self.updateOptions()

        self.authorized.emit(self.me.player)

        # Run an initial connectivity test and initialize a gamesession object
        # when done
        self.connectivity = ConnectivityHelper(self, self.gamePort)
        self.connectivity.connectivity_status_established.connect(self.initialize_game_session)
        self.connectivity.start_test()

    def initialize_game_session(self):
        self.game_session = GameSession(self, self.connectivity)

    def handle_registration_response(self, message):
        if message["result"] == "SUCCESS":
            return

        self.handle_notice({"style": "notice", "text": message["error"]})

    def search_ranked(self, faction):
        def request_launch():
            msg = {
                'command': 'game_matchmaking',
                'mod': 'ladder1v1',
                'state': 'start',
                'gameport': self.gamePort,
                'faction': faction
            }
            if self.connectivity.state == 'STUN':
                msg['relay_address'] = self.connectivity.relay_address
            self.lobby_connection.send(msg)
            self.game_session.ready.disconnect(request_launch)
        if self.game_session:
            self.game_session.ready.connect(request_launch)
            self.game_session.listen()

    def host_game(self, title, mod, visibility, mapname, password, is_rehost=False):
        def request_launch():
            msg = {
                'command': 'game_host',
                'title': title,
                'mod': mod,
                'visibility': visibility,
                'mapname': mapname,
                'password': password,
                'is_rehost': is_rehost
            }
            if self.connectivity.state == 'STUN':
                msg['relay_address'] = self.connectivity.relay_address
            self.lobby_connection.send(msg)
            self.game_session.ready.disconnect(request_launch)
        if self.game_session:
            self.game_session.game_password = password
            self.game_session.ready.connect(request_launch)
            self.game_session.listen()

    def join_game(self, uid, password=None):
        def request_launch():
            msg = {
                'command': 'game_join',
                'uid': uid,
                'gameport': self.gamePort
            }
            if password:
                msg['password'] = password
            if self.connectivity.state == "STUN":
                msg['relay_address'] = self.connectivity.relay_address
            self.lobby_connection.send(msg)
            self.game_session.ready.disconnect(request_launch)
        if self.game_session:
            self.game_session.game_password = password
            self.game_session.ready.connect(request_launch)
            self.game_session.listen()

    def handle_game_launch(self, message):
        if not self.game_session or not self.connectivity.is_ready:
            logger.error("Not ready for game launch")

        logger.info("Handling game_launch via JSON " + str(message))

        silent = False
        # Do some special things depending of the reason of the game launch.
        rank = False

        # HACK: Ideally, this comes from the server, too. LATER: search_ranked message
        arguments = []
        if message["mod"] == "ladder1v1":
            arguments.append('/' + Factions.to_name(self.games.race))
            # Player 1v1 rating
            arguments.append('/mean')
            arguments.append(str(self.me.player.ladder_rating_mean))
            arguments.append('/deviation')
            arguments.append(str(self.me.player.ladder_rating_deviation))
            arguments.append('/players 2')  # Always 2 players in 1v1 ladder
            arguments.append('/team 1')     # Always FFA team

            # Launch the auto lobby
            self.game_session.init_mode = 1

        else:
            # Player global rating
            arguments.append('/mean')
            arguments.append(str(self.me.player.rating_mean))
            arguments.append('/deviation')
            arguments.append(str(self.me.player.rating_deviation))
            if self.me.player.country is not None:
                arguments.append('/country ')
                arguments.append(self.me.player.country)

            # Launch the normal lobby
            self.game_session.init_mode = 0

        if self.me.player.clan is not None:
            arguments.append('/clan')
            arguments.append(self.me.player.clan)

        # Ensure we have the map
        if "mapname" in message:
            fa.check.map_(message['mapname'], force=True, silent=silent)

        if "sim_mods" in message:
            fa.mods.checkMods(message['sim_mods'])

        # UPnP Mapper - mappings are removed on app exit
        if self.useUPnP:
            self.lobby_connection.set_upnp(self.gamePort)

        info = dict(uid=message['uid'], recorder=self.login, featured_mod=message['mod'], launched_at=time.time())

        self.game_session.game_uid = message['uid']

        fa.run(info, self.game_session.relay_port, arguments)

    def fill_in_session_info(self, game):
        # sometimes we get the game_info message before a game session was created
        if self.game_session and game.uid == self.game_session.game_uid:
            self.game_session.game_map = game.mapname
            self.game_session.game_mod = game.featured_mod
            self.game_session.game_name = game.title
            self.game_session.game_visibility = game.visibility.value

    def handle_matchmaker_info(self, message):
        if not self.me.player:
            return
        if "action" in message:
            self.matchmakerInfo.emit(message)
        elif "queues" in message:
            if self.me.player.ladder_rating_deviation > 200 or self.games.searching:
                return
            key = 'boundary_80s' if self.me.player.ladder_rating_deviation < 100 else 'boundary_75s'
            show = False
            for q in message['queues']:
                if q['queue_name'] == 'ladder1v1':
                    mu = self.me.player.ladder_rating_mean
                    for min, max in q[key]:
                        if min < mu < max:
                            show = True
            if show:
                self.warningShow()
            else:
                self.warningHide()

    def handle_social(self, message):
        if "friends" in message:
            self.me.setFriends(set([int(u) for u in message["friends"]]))

        if "foes" in message:
            self.me.setFoes(set([int(u) for u in message["foes"]]))

        if "channels" in message:
            # Add a delay to the notification system (insane cargo cult)
            self.notificationSystem.disabledStartup = False
            self.channelsUpdated.emit(message["channels"])

        if "autojoin" in message:
            self.autoJoin.emit(message["autojoin"])

        if "power" in message:
            self.power = message["power"]
            self.manage_power()

    def handle_player_info(self, message):
        players = message["players"]

        # Fix id being a Python keyword
        for player in players:
            player["id_"] = player["id"]
            del player["id"]

        for player in players:
            id_ = int(player["id_"])
            if id_ in self.players:
                self.players[id_].update(**player)
            else:
                self.players[id_] = Player(**player)

    def avatarManager(self):
        self.requestAvatars(0)
        self.avatarSelection.show()

    def handle_authentication_failed(self, message):
        QtWidgets.QMessageBox.warning(self, "Authentication failed", message["text"])
        self._autorelogin = False
        self.get_creds_and_login()

    def handle_notice(self, message):
        if "text" in message:
            style = message.get('style', None)
            if style == "error":
                QtWidgets.QMessageBox.critical(self, "Error from Server", message["text"])
            elif style == "warning":
                QtWidgets.QMessageBox.warning(self, "Warning from Server", message["text"])
            elif style == "scores":
                self.tray.showMessage("Scores", message["text"], QtWidgets.QSystemTrayIcon.Information, 3500)
                self.localBroadcast.emit("Scores", message["text"])
            else:
                QtWidgets.QMessageBox.information(self, "Notice from Server", message["text"])

        if message["style"] == "kill":
            logger.info("Server has killed your Forged Alliance Process.")
            fa.instance.kill()

        if message["style"] == "kick":
            logger.info("Server has kicked you from the Lobby.")

        # This is part of the protocol - in this case we should not relogin automatically.
        if message["style"] in ["error", "kick"]:
            self._autorelogin = False

    def handle_invalid(self, message):
        # We did something wrong and the server will disconnect, let's not
        # reconnect and potentially cause the same error again and again
        self.lobby_reconnecter.enabled = False
        raise Exception(message)
